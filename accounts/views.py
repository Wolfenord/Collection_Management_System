from django.contrib import messages
from django.contrib.auth import get_user_model, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from .forms import ProfileForm, RegistrationForm, StyledPasswordChangeForm
from .throttling import ratelimit_post


def _notify_admins_of_registration(request, user) -> None:
    """E-mail every administrator that a registration awaits approval.

    Uses the configured e-mail backend; failures must never break the
    registration itself (fail_silently).
    """
    recipients = list(
        get_user_model().objects.filter(is_staff=True, is_active=True)
        .exclude(email='').values_list('email', flat=True)
    )
    if not recipients:
        return
    admin_url = request.build_absolute_uri(reverse('admin:accounts_user_changelist'))
    send_mail(
        subject=_('CMS: Neue Registrierung wartet auf Freigabe'),
        message=_('Der Benutzer „%(username)s“ (%(email)s) hat sich registriert und '
                  'wartet auf Freigabe.\n\nFreigeben oder ablehnen: %(url)s')
                % {'username': user.get_username(), 'email': user.email, 'url': admin_url},
        from_email=None,  # DEFAULT_FROM_EMAIL
        recipient_list=recipients,
        fail_silently=True,
    )


@ratelimit_post('register', max_requests=10, window_seconds=3600)
def register(request):
    """Self-service account creation.

    Can be switched off entirely via the runtime setting ``registration_enabled``.
    The new account is created locked and on the approval whitelist, so we do
    NOT log the user in. They have to wait for an administrator to approve it
    (unless ``registration_auto_approve`` is enabled).
    """
    if request.user.is_authenticated:
        return redirect('dashboard')

    from Collection_Management_System.runtime_settings import get_setting
    if not get_setting('registration_enabled'):
        messages.info(request, _('Die Registrierung ist derzeit geschlossen.'))
        return redirect('login')

    if request.method == 'POST':
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            if user.is_active:  # runtime setting registration_auto_approve
                messages.success(
                    request, _('Danke für deine Registrierung! Du kannst dich jetzt anmelden.'))
            else:
                if get_setting('notify_admins_on_registration'):
                    _notify_admins_of_registration(request, user)
                messages.success(
                    request,
                    _('Danke für deine Registrierung! Dein Konto muss noch von einem '
                      'Administrator freigegeben werden. Du kannst dich anmelden, '
                      'sobald die Freigabe erfolgt ist.'),
                )
            return redirect('login')
    else:
        form = RegistrationForm()

    return render(request, 'registration/register.html', {'form': form})


@login_required
def profile(request):
    """Edit one's own profile: display name, e-mail and personal overrides of
    per-user runtime settings (e.g. items per page). Also lists API tokens."""
    if request.method == 'POST':
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, _('Profil gespeichert.'))
            return redirect('profile')
    else:
        form = ProfileForm(instance=request.user)
    return render(request, 'accounts/profile.html', {
        'form': form,
        'api_tokens': request.user.api_tokens.all(),
        'user_passkeys': request.user.passkeys.all(),
    })


@login_required
def token_create(request):
    """Create a personal API token; the key is shown exactly once."""
    from .models import ApiToken
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()[:100] or _('API-Token')
        token, key = ApiToken.create_for_user(request.user, name)
        messages.success(
            request,
            _('Token „%(name)s“ erstellt: %(key)s — jetzt kopieren, '
              'er wird nur dieses eine Mal angezeigt.')
            % {'name': token.name, 'key': key},
        )
    return redirect('profile')


@login_required
def token_delete(request, token_pk):
    from .models import ApiToken
    if request.method == 'POST':
        token = ApiToken.objects.filter(pk=token_pk, user=request.user).first()
        if token:
            name = token.name
            token.delete()
            messages.success(request, _('Token „%(name)s“ widerrufen.') % {'name': name})
    return redirect('profile')


@login_required
def password_change(request):
    """Let a signed-in user change their own password (keeps the session alive)."""
    if request.method == 'POST':
        form = StyledPasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            messages.success(request, _('Dein Passwort wurde geändert.'))
            return redirect('dashboard')
    else:
        form = StyledPasswordChangeForm(request.user)
    return render(request, 'registration/password_change.html', {'form': form})
