from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.forms import (
    AuthenticationForm,
    PasswordChangeForm,
    PasswordResetForm,
    SetPasswordForm,
    UserCreationForm,
)
from django.utils.translation import gettext_lazy as _

from .models import User


class _BootstrapFormMixin:
    """Adds Bootstrap widget classes to every field of a stock Django form."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')


class StyledPasswordChangeForm(_BootstrapFormMixin, PasswordChangeForm):
    pass


class StyledPasswordResetForm(_BootstrapFormMixin, PasswordResetForm):
    pass


class StyledSetPasswordForm(_BootstrapFormMixin, SetPasswordForm):
    pass


class RegistrationForm(UserCreationForm):
    """Sign-up form: username, e-mail and password (with confirmation).

    By default a new account is created locked (``is_active=False``) and
    ``pending`` so it has to be approved by an administrator before the user
    can log in. If the runtime setting ``registration_auto_approve`` is enabled
    the account is approved immediately instead.
    """

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['email'].required = True
        # Bootstrap styling for all fields.
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def save(self, commit=True):
        from Collection_Management_System.runtime_settings import get_setting

        user = super().save(commit=False)
        if get_setting('registration_auto_approve'):
            user.is_active = True
            user.approval_status = User.APPROVAL_APPROVED
        else:
            user.is_active = False
            user.approval_status = User.APPROVAL_PENDING
        if commit:
            user.save()
        return user


class ProfileForm(forms.ModelForm):
    """Profile page: display name, e-mail and personal setting overrides.

    One extra input is generated for every runtime setting marked ``per_user``
    in the registry (currently the item-table page size). An empty input means
    "use the site-wide value" and removes the stored override.
    """

    class Meta:
        model = User
        fields = ('display_name', 'email')

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from Collection_Management_System import runtime_settings

        self.per_user_defs = [d for d in runtime_settings.REGISTRY.values() if d.per_user]
        preferences = self.instance.preferences or {}
        for definition in self.per_user_defs:
            # per_user settings are ints today; extend like SiteSettingsForm
            # if other kinds ever get the flag.
            self.fields[definition.key] = forms.IntegerField(
                required=False, label=definition.label,
                min_value=definition.min_value, max_value=definition.max_value,
                initial=preferences.get(definition.key),
                help_text=_('Leer = Standardwert der Anwendung (%(value)s).')
                          % {'value': runtime_settings.get_setting(definition.key)},
            )
        for field in self.fields.values():
            field.widget.attrs.setdefault('class', 'form-control')

    def save(self, commit=True):
        user = super().save(commit=False)
        preferences = dict(user.preferences or {})
        for definition in self.per_user_defs:
            value = self.cleaned_data.get(definition.key)
            if value in (None, ''):
                preferences.pop(definition.key, None)
            else:
                preferences[definition.key] = value
        user.preferences = preferences
        if commit:
            user.save()
        return user


class ApprovalAuthenticationForm(AuthenticationForm):
    """Login form that explains *why* an unapproved account can't sign in.

    Django's ``ModelBackend`` already refuses to authenticate inactive users, so
    ``authenticate()`` returns ``None`` for a pending/rejected account exactly
    like it does for a wrong password. To give honest feedback we re-check the
    password ourselves on the failure path: only a request that supplied the
    *correct* password is told the account is pending/rejected. Wrong-password
    attempts still get the generic error, so this leaks no account information.
    """

    def confirm_login_allowed(self, user):
        if user.approval_status == User.APPROVAL_PENDING:
            raise forms.ValidationError(
                _('Dein Konto wurde noch nicht freigegeben. Ein Administrator '
                  'muss deine Registrierung erst bestätigen.'),
                code='pending',
            )
        if user.approval_status == User.APPROVAL_REJECTED:
            raise forms.ValidationError(
                _('Deine Registrierung wurde abgelehnt.'),
                code='rejected',
            )
        super().confirm_login_allowed(user)

    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username and password:
            self.user_cache = authenticate(
                self.request, username=username, password=password,
            )
            if self.user_cache is None:
                # authenticate() returns None for both wrong credentials and
                # inactive (unapproved) accounts. Look the account up and verify
                # the password to give a precise reason — but only to someone
                # who actually knows the password.
                UserModel = get_user_model()
                try:
                    user = UserModel._default_manager.get_by_natural_key(username)
                except UserModel.DoesNotExist:
                    user = None
                if user is not None and user.check_password(password) and not user.is_active:
                    self.confirm_login_allowed(user)
                raise self.get_invalid_login_error()
            else:
                self.confirm_login_allowed(self.user_cache)

        return self.cleaned_data
