"""Passkey (WebAuthn/FIDO2) registration and login.

Four small JSON endpoints driven by ``static/js/passkeys.js``:

* register_begin/register_complete — a signed-in user adds a passkey on the
  profile page. The challenge lives in the session between the two calls.
* login_begin/login_complete — password-less sign-in from the login page.
  We ask for *discoverable* credentials, so the browser offers the stored
  passkeys for this site and no username has to be typed.

Security notes: the challenge is single-use (popped from the session),
origin and RP-ID are verified by py_webauthn, user verification (PIN /
biometrics on the authenticator) is required for login, the sign counter
guards against cloned authenticators, and the account must be active and
approved — the registration whitelist applies to passkey logins too.
Browsers only expose the WebAuthn API in secure contexts (HTTPS or
localhost), so production needs TLS anyway (see SECURITY.md).
"""

import json
import logging

from django.contrib.auth import login as auth_login
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import bytes_to_base64url
from webauthn.helpers.exceptions import (
    InvalidAuthenticationResponse,
    InvalidJSONStructure,
    InvalidRegistrationResponse,
)
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .models import WebAuthnCredential
from .throttling import ratelimit_post

security_log = logging.getLogger('cms.security')

RP_NAME = 'Collection Management System'
_REG_CHALLENGE_KEY = 'webauthn_register_challenge'
_AUTH_CHALLENGE_KEY = 'webauthn_login_challenge'


def _rp_id(request) -> str:
    """The relying-party ID is the bare hostname (no port, no scheme)."""
    return request.get_host().split(':')[0]


def _origin(request) -> str:
    return f'{request.scheme}://{request.get_host()}'


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({'ok': False, 'error': message}, status=status)


@login_required
@require_POST
def register_begin(request):
    options = generate_registration_options(
        rp_id=_rp_id(request),
        rp_name=RP_NAME,
        user_id=str(request.user.pk).encode(),
        user_name=request.user.get_username(),
        user_display_name=str(request.user),
        # Discoverable credential + user verification → a real passkey that
        # can later sign in without a typed username.
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED,
            user_verification=UserVerificationRequirement.PREFERRED,
        ),
        exclude_credentials=[
            PublicKeyCredentialDescriptor(id=base64url_to_bytes(cred.credential_id))
            for cred in request.user.passkeys.all()
        ],
    )
    request.session[_REG_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return JsonResponse(json.loads(options_to_json(options)))


@login_required
@require_POST
def register_complete(request):
    challenge = request.session.pop(_REG_CHALLENGE_KEY, None)
    if not challenge:
        return _error(_('Keine laufende Passkey-Registrierung.'))
    try:
        payload = json.loads(request.body)
        verification = verify_registration_response(
            credential=payload['credential'],
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
        )
    except (KeyError, ValueError, InvalidJSONStructure, InvalidRegistrationResponse):
        return _error(_('Passkey-Registrierung fehlgeschlagen.'))

    label = (payload.get('label') or '').strip()[:100] or _('Passkey')
    WebAuthnCredential.objects.create(
        user=request.user,
        label=label,
        credential_id=bytes_to_base64url(verification.credential_id),
        public_key=bytes_to_base64url(verification.credential_public_key),
        sign_count=verification.sign_count,
    )
    return JsonResponse({'ok': True})


@require_POST
def login_begin(request):
    options = generate_authentication_options(
        rp_id=_rp_id(request),
        # Empty allow-list → the browser offers the discoverable credentials
        # it holds for this site; no username needed, none is leaked.
        allow_credentials=[],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    request.session[_AUTH_CHALLENGE_KEY] = bytes_to_base64url(options.challenge)
    return JsonResponse(json.loads(options_to_json(options)))


@ratelimit_post('passkey', max_requests=20, window_seconds=900)
@require_POST
def login_complete(request):
    challenge = request.session.pop(_AUTH_CHALLENGE_KEY, None)
    if not challenge:
        return _error(_('Keine laufende Passkey-Anmeldung.'))
    try:
        payload = json.loads(request.body)
        credential = payload['credential']
        stored = WebAuthnCredential.objects.select_related('user').get(
            credential_id=credential['id'],
        )
    except (KeyError, ValueError, WebAuthnCredential.DoesNotExist):
        # Same generic error as for a failed verification: an attacker must
        # not learn whether a credential ID exists.
        return _error(_('Passkey-Anmeldung fehlgeschlagen.'))

    try:
        verification = verify_authentication_response(
            credential=credential,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=_rp_id(request),
            expected_origin=_origin(request),
            credential_public_key=base64url_to_bytes(stored.public_key),
            credential_current_sign_count=stored.sign_count,
            require_user_verification=True,
        )
    except (InvalidJSONStructure, InvalidAuthenticationResponse):
        security_log.warning('Passkey login verification failed: user=%r ip=%s',
                             stored.user.get_username(),
                             request.META.get('REMOTE_ADDR'))
        return _error(_('Passkey-Anmeldung fehlgeschlagen.'))

    user = stored.user
    if not user.is_active:
        # Approval whitelist / deactivated accounts apply to passkeys too.
        security_log.warning('Passkey login for inactive account blocked: user=%r',
                             user.get_username())
        return _error(_('Dieses Konto ist nicht freigegeben oder deaktiviert.'), status=403)

    WebAuthnCredential.objects.filter(pk=stored.pk).update(
        sign_count=verification.new_sign_count, last_used_at=timezone.now(),
    )
    auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')

    next_url = request.POST.get('next') or payload.get('next') or ''
    from django.utils.http import url_has_allowed_host_and_scheme
    if not next_url or not url_has_allowed_host_and_scheme(
            next_url, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        next_url = reverse('dashboard')
    return JsonResponse({'ok': True, 'redirect': next_url})


@login_required
@require_POST
def passkey_delete(request, passkey_pk):
    passkey = WebAuthnCredential.objects.filter(pk=passkey_pk, user=request.user).first()
    if passkey:
        from django.contrib import messages
        name = passkey.label
        passkey.delete()
        messages.success(request, _('Passkey „%(name)s“ entfernt.') % {'name': name})
    return redirect('profile')
