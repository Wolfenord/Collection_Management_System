from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


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

    def test_create_token_shows_key_once(self):
        from .models import ApiToken
        resp = self.client.post(reverse('token_create'), {'name': 'NAS-Skript'},
                                follow=True)
        token = ApiToken.objects.get(user=self.user)
        self.assertEqual(token.name, 'NAS-Skript')
        self.assertContains(resp, token.key)      # shown once after creation
        resp = self.client.get(reverse('profile'))
        self.assertNotContains(resp, token.key)   # never again afterwards

    def test_revoke_own_token(self):
        from .models import ApiToken
        token = ApiToken.objects.create(user=self.user, name='Alt')
        self.client.post(reverse('token_delete', args=[token.pk]))
        self.assertFalse(ApiToken.objects.filter(pk=token.pk).exists())

    def test_cannot_revoke_foreign_token(self):
        from .models import ApiToken
        other = User.objects.create_user('other', 'x@e.de', 'pw')
        token = ApiToken.objects.create(user=other, name='Fremd')
        self.client.post(reverse('token_delete', args=[token.pk]))
        self.assertTrue(ApiToken.objects.filter(pk=token.pk).exists())
