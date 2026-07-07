from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase as DjangoTestCase
from django.urls import reverse

User = get_user_model()


class TestCase(DjangoTestCase):
    """Project TestCase: start every test with an empty cache so the
    brute-force throttle counters (accounts.throttling) never leak between
    tests. ``_pre_setup`` runs before each test even when subclasses define
    their own ``setUp`` without calling ``super()``."""

    def _pre_setup(self):
        super()._pre_setup()
        cache.clear()


class RegistrationApprovalTests(TestCase):
    """Self-service registration goes onto an admin-approval whitelist."""

    def _register(self):
        return self.client.post(reverse('register'), {
            'username': 'neuer',
            'email': 'neuer@example.com',
            'password1': 'sicheres-passwort-123',
            'password2': 'sicheres-passwort-123',
        })

    def test_registration_creates_pending_inactive_user_not_logged_in(self):
        resp = self._register()
        self.assertRedirects(resp, reverse('login'))

        user = User.objects.get(username='neuer')
        self.assertFalse(user.is_active)
        self.assertEqual(user.approval_status, User.APPROVAL_PENDING)
        # No auto-login: the session must be anonymous.
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_pending_user_cannot_log_in_and_sees_reason(self):
        self._register()
        resp = self.client.post(reverse('login'), {
            'username': 'neuer', 'password': 'sicheres-passwort-123',
        })
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(resp, 'noch nicht freigegeben')

    def test_wrong_password_gives_generic_error_not_pending_leak(self):
        self._register()
        resp = self.client.post(reverse('login'), {
            'username': 'neuer', 'password': 'falsches-passwort',
        })
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertNotContains(resp, 'noch nicht freigegeben')

    def test_approved_user_can_log_in(self):
        self._register()
        user = User.objects.get(username='neuer')
        admin = User.objects.create_superuser('admin', 'a@a.de', 'pw')
        user.approve(by=admin)

        self.assertTrue(user.is_active)
        self.assertEqual(user.approval_status, User.APPROVAL_APPROVED)
        self.assertEqual(user.approved_by, admin)
        self.assertIsNotNone(user.approval_decided_at)

        ok = self.client.login(username='neuer', password='sicheres-passwort-123')
        self.assertTrue(ok)

    def test_rejected_user_cannot_log_in_and_sees_reason(self):
        self._register()
        user = User.objects.get(username='neuer')
        user.reject()
        self.assertFalse(user.is_active)
        self.assertEqual(user.approval_status, User.APPROVAL_REJECTED)

        resp = self.client.post(reverse('login'), {
            'username': 'neuer', 'password': 'sicheres-passwort-123',
        })
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertContains(resp, 'abgelehnt')

    def test_admin_approve_action_whitelists_users(self):
        self._register()
        admin = User.objects.create_superuser('admin', 'a@a.de', 'pw')
        self.client.force_login(admin)

        pending = User.objects.get(username='neuer')
        resp = self.client.post(reverse('admin:accounts_user_changelist'), {
            'action': 'approve_users',
            '_selected_action': [str(pending.pk)],
        }, follow=True)
        self.assertEqual(resp.status_code, 200)

        pending.refresh_from_db()
        self.assertTrue(pending.is_active)
        self.assertEqual(pending.approval_status, User.APPROVAL_APPROVED)
        self.assertEqual(pending.approved_by, admin)


class PasswordChangeTests(TestCase):
    """Signed-in users can change their own password."""

    def setUp(self):
        self.user = User.objects.create_user('anna', 'anna@e.de', 'altes-passwort-123')
        self.url = reverse('password_change')

    def test_requires_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse('login'), resp['Location'])

    def test_change_password_keeps_session(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.url, {
            'old_password': 'altes-passwort-123',
            'new_password1': 'ganz-neues-passwort-456',
            'new_password2': 'ganz-neues-passwort-456',
        })
        self.assertRedirects(resp, reverse('dashboard'))
        # Session survives the hash change (update_session_auth_hash).
        self.assertIn('_auth_user_id', self.client.session)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('ganz-neues-passwort-456'))

    def test_wrong_old_password_rejected(self):
        self.client.force_login(self.user)
        resp = self.client.post(self.url, {
            'old_password': 'falsch',
            'new_password1': 'ganz-neues-passwort-456',
            'new_password2': 'ganz-neues-passwort-456',
        })
        self.assertEqual(resp.status_code, 200)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('altes-passwort-123'))


class PasswordResetTests(TestCase):
    """Forgotten-password flow: request mail, follow link, set new password."""

    def setUp(self):
        self.user = User.objects.create_user('anna', 'anna@example.com', 'altes-passwort-123')

    def test_login_page_links_reset(self):
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, reverse('password_reset'))

    def test_full_reset_flow(self):
        from django.core import mail
        resp = self.client.post(reverse('password_reset'), {'email': 'anna@example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('anna@example.com', mail.outbox[0].to)

        # Extract the confirm link from the mail body and follow it.
        import re
        match = re.search(r'/accounts/reset/[^/]+/[^/\s]+/', mail.outbox[0].body)
        self.assertIsNotNone(match)
        resp = self.client.get(match.group(0), follow=True)
        self.assertEqual(resp.status_code, 200)
        # Django redirects to a session-bound URL for the actual form.
        set_url = resp.redirect_chain[-1][0]
        resp = self.client.post(set_url, {
            'new_password1': 'ganz-neues-passwort-456',
            'new_password2': 'ganz-neues-passwort-456',
        })
        self.assertRedirects(resp, reverse('password_reset_complete'))
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('ganz-neues-passwort-456'))

    def test_unknown_email_reveals_nothing(self):
        from django.core import mail
        resp = self.client.post(reverse('password_reset'), {'email': 'gibtsnicht@example.com'})
        self.assertRedirects(resp, reverse('password_reset_done'))
        self.assertEqual(len(mail.outbox), 0)

    def test_inactive_pending_user_gets_no_mail(self):
        from django.core import mail
        pending = User.objects.create_user('neu', 'neu@example.com', 'pw')
        pending.is_active = False
        pending.save()
        self.client.post(reverse('password_reset'), {'email': 'neu@example.com'})
        self.assertEqual(len(mail.outbox), 0)


class AutoApproveRegistrationTests(TestCase):
    """With the runtime setting ``registration_auto_approve`` enabled, new
    accounts skip the approval whitelist and can log in immediately."""

    def setUp(self):
        from django.core.cache import cache
        from Collection_Management_System import runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        runtime_settings.set_setting('registration_auto_approve', True)

    def _register(self):
        return self.client.post(reverse('register'), {
            'username': 'sofort',
            'email': 'sofort@example.com',
            'password1': 'sicheres-passwort-123',
            'password2': 'sicheres-passwort-123',
        })

    def test_registration_creates_active_approved_user(self):
        resp = self._register()
        self.assertRedirects(resp, reverse('login'))
        user = User.objects.get(username='sofort')
        self.assertTrue(user.is_active)
        self.assertEqual(user.approval_status, User.APPROVAL_APPROVED)

    def test_user_can_log_in_immediately(self):
        self._register()
        self.client.post(reverse('login'), {
            'username': 'sofort', 'password': 'sicheres-passwort-123',
        })
        self.assertIn('_auth_user_id', self.client.session)


class RegistrationClosedTests(TestCase):
    """With ``registration_enabled`` off, no new accounts can be created."""

    def setUp(self):
        from django.core.cache import cache
        from Collection_Management_System import runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        runtime_settings.set_setting('registration_enabled', False)

    def test_get_redirects_to_login(self):
        resp = self.client.get(reverse('register'))
        self.assertRedirects(resp, reverse('login'))

    def test_post_creates_no_account(self):
        resp = self.client.post(reverse('register'), {
            'username': 'zuspaet',
            'email': 'zuspaet@example.com',
            'password1': 'sicheres-passwort-123',
            'password2': 'sicheres-passwort-123',
        })
        self.assertRedirects(resp, reverse('login'))
        self.assertFalse(User.objects.filter(username='zuspaet').exists())

    def test_register_links_hidden(self):
        resp = self.client.get(reverse('login'))
        self.assertNotContains(resp, reverse('register'))


class RegistrationAdminNotificationTests(TestCase):
    """Optional e-mail to administrators when a registration awaits approval."""

    def setUp(self):
        from django.core.cache import cache
        from Collection_Management_System import runtime_settings
        self.rs = runtime_settings
        cache.delete(runtime_settings._CACHE_KEY)
        self.addCleanup(cache.delete, runtime_settings._CACHE_KEY)
        self.admin = User.objects.create_user(
            'admin', 'admin@example.com', 'pw', is_staff=True)

    def _register(self, username='neuling'):
        return self.client.post(reverse('register'), {
            'username': username,
            'email': f'{username}@example.com',
            'password1': 'sicheres-passwort-123',
            'password2': 'sicheres-passwort-123',
        })

    def test_no_mail_by_default(self):
        from django.core import mail
        self._register()
        self.assertEqual(len(mail.outbox), 0)

    def test_mail_sent_to_staff_when_enabled(self):
        from django.core import mail
        self.rs.set_setting('notify_admins_on_registration', True)
        self._register()
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('admin@example.com', mail.outbox[0].to)
        self.assertIn('neuling', mail.outbox[0].body)
        self.assertIn('/admin/', mail.outbox[0].body)

    def test_no_mail_for_auto_approved_accounts(self):
        from django.core import mail
        self.rs.set_setting('notify_admins_on_registration', True)
        self.rs.set_setting('registration_auto_approve', True)
        self._register()
        self.assertEqual(len(mail.outbox), 0)

    def test_staff_without_email_skipped(self):
        from django.core import mail
        self.admin.email = ''
        self.admin.save(update_fields=['email'])
        self.rs.set_setting('notify_admins_on_registration', True)
        self._register()
        self.assertEqual(len(mail.outbox), 0)


class ApiTokenUiTests(TestCase):
    """Token management on the profile page."""

    def setUp(self):
        self.user = User.objects.create_user('u', 'u@e.de', 'pw')
        self.client.force_login(self.user)

    def test_create_token_shows_key_once_and_stores_only_hash(self):
        import re
        from .models import ApiToken
        resp = self.client.post(reverse('token_create'), {'name': 'NAS-Skript'},
                                follow=True)
        token = ApiToken.objects.get(user=self.user)
        self.assertEqual(token.name, 'NAS-Skript')
        # The plain key appears exactly once, in the success message …
        match = re.search(r'erstellt: ([\w-]+)', resp.content.decode())
        self.assertIsNotNone(match)
        key = match.group(1)
        # … and the database holds only its hash.
        self.assertEqual(token.key_hash, ApiToken.hash_key(key))
        self.assertNotEqual(token.key_hash, key)
        resp = self.client.get(reverse('profile'))
        self.assertNotContains(resp, key)   # never shown again afterwards

    def test_revoke_own_token(self):
        from .models import ApiToken
        token, _key = ApiToken.create_for_user(self.user, 'Alt')
        self.client.post(reverse('token_delete', args=[token.pk]))
        self.assertFalse(ApiToken.objects.filter(pk=token.pk).exists())

    def test_cannot_revoke_foreign_token(self):
        from .models import ApiToken
        other = User.objects.create_user('other', 'x@e.de', 'pw')
        token, _key = ApiToken.create_for_user(other, 'Fremd')
        self.client.post(reverse('token_delete', args=[token.pk]))
        self.assertTrue(ApiToken.objects.filter(pk=token.pk).exists())


class BruteForceThrottleTests(TestCase):
    """Cache-based lockouts on login, registration and password reset."""

    def setUp(self):
        self.user = User.objects.create_user(
            'anna', 'anna@e.de', 'richtig-123',
            approval_status=User.APPROVAL_APPROVED,
        )

    def _fail_login(self):
        return self.client.post(reverse('login'),
                                {'username': 'anna', 'password': 'falsch'})

    def test_login_locks_after_five_failures(self):
        from .throttling import LOGIN_MAX_PER_USER
        for _ in range(LOGIN_MAX_PER_USER):
            self._fail_login()
        resp = self.client.post(reverse('login'),
                                {'username': 'anna', 'password': 'richtig-123'})
        # Even the CORRECT password is refused while locked.
        self.assertContains(resp, 'vorübergehend gesperrt')
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_successful_login_resets_counter(self):
        for _ in range(3):
            self._fail_login()
        self.client.post(reverse('login'),
                         {'username': 'anna', 'password': 'richtig-123'})
        self.assertIn('_auth_user_id', self.client.session)

    def test_register_rate_limited_per_ip(self):
        url = reverse('register')
        for i in range(10):
            self.client.post(url, {'username': f'benutzer{i}'})  # invalid is fine
        resp = self.client.post(url, {'username': 'benutzer11'})
        self.assertEqual(resp.status_code, 429)

    def test_password_reset_rate_limited_per_ip(self):
        url = reverse('password_reset')
        for _ in range(5):
            self.client.post(url, {'email': 'anna@e.de'})
        resp = self.client.post(url, {'email': 'anna@e.de'})
        self.assertEqual(resp.status_code, 429)

    def test_get_requests_never_throttled(self):
        for _ in range(30):
            resp = self.client.get(reverse('register'))
        self.assertEqual(resp.status_code, 200)


class PasskeyTests(TestCase):
    """Passkey (WebAuthn) registration and password-less login."""

    def setUp(self):
        self.user = User.objects.create_user(
            'anna', 'anna@e.de', 'pw-123',
            approval_status=User.APPROVAL_APPROVED,
        )

    def _credential(self, user=None, cid='dGVzdC1pZA'):
        from .models import WebAuthnCredential
        return WebAuthnCredential.objects.create(
            user=user or self.user, label='Handy',
            credential_id=cid, public_key='cHVibGljLWtleQ', sign_count=1,
        )

    def test_register_begin_requires_login_and_returns_options(self):
        url = reverse('passkey_register_begin')
        self.assertEqual(self.client.post(url).status_code, 302)  # → login

        self.client.force_login(self.user)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        options = resp.json()
        self.assertIn('challenge', options)
        self.assertEqual(options['rp']['id'], 'testserver')
        self.assertEqual(options['user']['name'], 'anna')
        # Challenge parked in the session for the complete step.
        self.assertIn('webauthn_register_challenge', self.client.session)

    def test_register_complete_rejects_garbage_and_missing_challenge(self):
        self.client.force_login(self.user)
        url = reverse('passkey_register_complete')
        # No begin call → no challenge in the session.
        resp = self.client.post(url, '{}', content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        # With a challenge but an invalid credential payload.
        self.client.post(reverse('passkey_register_begin'))
        resp = self.client.post(url, '{"credential": {"bad": true}}',
                                content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.user.passkeys.count(), 0)

    def test_login_begin_anonymous_returns_options(self):
        resp = self.client.post(reverse('passkey_login_begin'))
        self.assertEqual(resp.status_code, 200)
        options = resp.json()
        self.assertIn('challenge', options)
        self.assertEqual(options['userVerification'], 'required')
        self.assertIn('webauthn_login_challenge', self.client.session)

    def test_login_complete_unknown_credential_generic_error(self):
        self.client.post(reverse('passkey_login_begin'))
        resp = self.client.post(
            reverse('passkey_login_complete'),
            '{"credential": {"id": "gibtsnicht"}}',
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_complete_signs_in_active_user(self):
        from unittest import mock
        from accounts import passkeys as pk
        cred = self._credential()
        self.client.post(reverse('passkey_login_begin'))
        verified = mock.Mock(new_sign_count=2)
        with mock.patch.object(pk, 'verify_authentication_response', return_value=verified):
            resp = self.client.post(
                reverse('passkey_login_complete'),
                '{"credential": {"id": "%s"}}' % cred.credential_id,
                content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()['ok'])
        self.assertIn('_auth_user_id', self.client.session)
        cred.refresh_from_db()
        self.assertEqual(cred.sign_count, 2)   # clone detection counter moved on
        self.assertIsNotNone(cred.last_used_at)

    def test_login_complete_blocks_unapproved_account(self):
        from unittest import mock
        from accounts import passkeys as pk
        pending = User.objects.create_user('neu', 'n@e.de', 'pw', is_active=False)
        cred = self._credential(user=pending)
        self.client.post(reverse('passkey_login_begin'))
        with mock.patch.object(pk, 'verify_authentication_response',
                               return_value=mock.Mock(new_sign_count=2)):
            resp = self.client.post(
                reverse('passkey_login_complete'),
                '{"credential": {"id": "%s"}}' % cred.credential_id,
                content_type='application/json')
        self.assertEqual(resp.status_code, 403)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_login_complete_rejects_invalid_signature(self):
        cred = self._credential()
        self.client.post(reverse('passkey_login_begin'))
        # Real verification with a fake credential must fail cleanly.
        resp = self.client.post(
            reverse('passkey_login_complete'),
            '{"credential": {"id": "%s"}}' % cred.credential_id,
            content_type='application/json')
        self.assertEqual(resp.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_delete_only_own_passkey(self):
        other = User.objects.create_user('other', 'o@e.de', 'pw')
        foreign = self._credential(user=other, cid='ZnJlbWQ')
        mine = self._credential(cid='bWVpbnM')
        self.client.force_login(self.user)
        from .models import WebAuthnCredential
        self.client.post(reverse('passkey_delete', args=[foreign.pk]))
        self.assertTrue(WebAuthnCredential.objects.filter(pk=foreign.pk).exists())
        self.client.post(reverse('passkey_delete', args=[mine.pk]))
        self.assertFalse(WebAuthnCredential.objects.filter(pk=mine.pk).exists())

    def test_login_page_offers_passkey_button(self):
        resp = self.client.get(reverse('login'))
        self.assertContains(resp, 'id="passkeyLogin"')
        self.assertContains(resp, reverse('passkey_login_begin'))
        self.assertContains(resp, 'js/passkeys.js')

    def test_profile_page_offers_passkey_management(self):
        self.client.force_login(self.user)
        self._credential()
        resp = self.client.get(reverse('profile'))
        self.assertContains(resp, 'id="passkeyAdd"')
        self.assertContains(resp, 'Handy')
        self.assertContains(resp, reverse('passkey_register_begin'))


class PasswordHashingTests(TestCase):
    def test_new_passwords_use_argon2(self):
        user = User.objects.create_user('hash-test', 'h@e.de', 'ein-passwort-123')
        self.assertTrue(user.password.startswith('argon2'), user.password[:20])
