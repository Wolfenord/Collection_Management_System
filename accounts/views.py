from django.contrib import messages
from django.contrib.auth import get_user_model, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.core.mail import send_mail
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _

from .forms import ProfileForm, RegistrationForm, StyledPasswordChangeForm
from .throttling import ratelimit_post, security_log


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
                # In-app bell notification for every administrator (always);
                # e-mail additionally when the runtime setting is enabled.
                from Collection_Management_System.models import Notification
                admin_url = reverse('admin:accounts_user_changelist')
                for staff in (get_user_model().objects
                              .filter(is_staff=True, is_active=True)):
                    Notification.push(
                        staff, kind=Notification.KIND_REGISTRATION,
                        key=f'registration:{user.pk}', url=admin_url,
                        payload={'username': user.get_username()})
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


# --- GDPR self-service: data export (Art. 20) & account deletion (Art. 17) ------

@login_required
def data_export(request):
    """Download every piece of personal data as machine-readable JSON
    (GDPR Art. 15/20). Uploaded files are referenced by path; they can be
    downloaded from the item pages themselves (or per collection via the ZIP
    backup)."""
    from Collection_Management_System.export import collection_json
    from Collection_Management_System.models import Item
    from .throttling import allow

    # The export walks every owned item — cap it so a scripted client cannot
    # turn the heaviest read endpoint into a load generator.
    if not allow('data_export', str(request.user.pk), max_requests=10, window_seconds=3600):
        return HttpResponse(
            _('Zu viele Export-Anfragen. Bitte versuche es später erneut.'),
            status=429, content_type='text/plain; charset=utf-8')

    user = request.user
    foreign_items = (
        Item.all_objects.filter(created_by=user)
        .exclude(collection__owner=user)
        .select_related('collection')
    )
    data = {
        'exported_at': timezone.now().isoformat(),
        'account': {
            'username': user.get_username(),
            'display_name': user.display_name,
            'email': user.email,
            'date_joined': user.date_joined.isoformat(),
            'last_login': user.last_login.isoformat() if user.last_login else None,
            'preferences': user.preferences,
        },
        'collections': [collection_json(c) for c in user.collections.all()],
        'shared_with_me': [
            {'collection': share.collection.name,
             'owner': share.collection.owner.get_username(),
             'permission': share.permission}
            for share in user.shared_collections.select_related('collection__owner')
        ],
        'items_created_in_shared_collections': [
            {'collection': item.collection.name, 'id': str(item.pk), 'values': item.values}
            for item in foreign_items
        ],
        'api_tokens': [{'name': t.name, 'created_at': t.created_at.isoformat()}
                       for t in user.api_tokens.all()],
        'passkeys': [{'label': p.label, 'created_at': p.created_at.isoformat()}
                     for p in user.passkeys.all()],
    }
    response = JsonResponse(data, json_dumps_params={'ensure_ascii': False, 'indent': 2})
    response['Content-Disposition'] = (
        f'attachment; filename="cms-daten-{user.get_username()}.json"')
    return response


@login_required
@ratelimit_post('account_delete', max_requests=5, window_seconds=3600)
def account_delete(request):
    """Self-service account deletion (GDPR Art. 17).

    Deletes the account with every owned collection (items, uploaded files,
    loans, shares) after re-authentication — password, or typing the username
    for passkey-only accounts. Items the user created in *other people's*
    collections stay (they belong to those collections) but lose the personal
    reference (``created_by`` becomes NULL).
    """
    from Collection_Management_System.models import Item, ItemAsset

    user = request.user
    sole_superuser = user.is_superuser and not (
        get_user_model().objects.filter(is_superuser=True, is_active=True)
        .exclude(pk=user.pk).exists()
    )
    if request.method == 'POST' and not sole_superuser:
        if user.has_usable_password():
            confirmed = user.check_password(request.POST.get('password') or '')
            error = _('Das Passwort ist nicht korrekt.')
        else:
            confirmed = (request.POST.get('confirm') or '').strip() == user.get_username()
            error = _('Bitte gib zur Bestätigung deinen Benutzernamen exakt ein.')
        if not confirmed:
            messages.error(request, error)
        else:
            username = user.get_username()
            # Remove uploaded files from disk first — the DB cascade that
            # follows only deletes the rows, not the file storage.
            for asset in ItemAsset.objects.filter(item__collection__owner=user):
                asset.file.delete(save=False)
            logout(request)
            user.delete()
            security_log.info('Account self-deleted: user=%r', username)
            messages.success(request, _('Dein Konto und alle deine Sammlungen wurden '
                                        'endgültig gelöscht.'))
            return redirect('login')

    return render(request, 'registration/account_delete.html', {
        'sole_superuser': sole_superuser,
        'needs_password': user.has_usable_password(),
        'collection_count': user.collections.count(),
        'item_count': Item.all_objects.filter(collection__owner=user).count(),
    })


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
