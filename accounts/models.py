import secrets

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class User(AbstractUser):
    """Custom user model.

    Extending AbstractUser from the start keeps the project flexible: profile
    fields, preferences (e.g. UI language, dashboard layout) or avatars can be
    added later without a painful user-model swap.

    New self-service registrations land on an approval whitelist: the account
    is created *inactive* (``is_active=False``) with ``approval_status`` set to
    ``pending`` and cannot log in until an administrator approves it. We gate on
    Django's native ``is_active`` flag so the lock is enforced by the auth
    backend itself; ``approval_status`` adds the human-readable workflow state
    (and lets us tell "awaiting approval" apart from "deactivated/banned").
    """

    APPROVAL_PENDING = 'pending'
    APPROVAL_APPROVED = 'approved'
    APPROVAL_REJECTED = 'rejected'
    APPROVAL_CHOICES = [
        (APPROVAL_PENDING, _('Wartet auf Freigabe')),
        (APPROVAL_APPROVED, _('Freigegeben')),
        (APPROVAL_REJECTED, _('Abgelehnt')),
    ]

    display_name = models.CharField(_('Anzeigename'), max_length=150, blank=True)

    # Personal overrides of per_user runtime settings, e.g. {'items_per_page': 25}.
    # Keys/values are validated against runtime_settings.REGISTRY on save (profile
    # form) and again on read (get_setting_for), so stale entries can't break views.
    preferences = models.JSONField(_('Einstellungen'), default=dict, blank=True)

    approval_status = models.CharField(
        _('Freigabestatus'),
        max_length=10,
        choices=APPROVAL_CHOICES,
        default=APPROVAL_PENDING,
        db_index=True,
    )
    approval_requested_at = models.DateTimeField(
        _('Registriert am'), default=timezone.now,
    )
    approval_decided_at = models.DateTimeField(
        _('Entschieden am'), null=True, blank=True,
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='approval_decisions',
        verbose_name=_('Entschieden von'),
    )

    def __str__(self) -> str:
        return self.display_name or self.get_username()

    @property
    def is_pending_approval(self) -> bool:
        return self.approval_status == self.APPROVAL_PENDING

    def approve(self, by=None):
        """Add the user to the whitelist: mark approved and allow login."""
        self.approval_status = self.APPROVAL_APPROVED
        self.is_active = True
        self.approval_decided_at = timezone.now()
        self.approved_by = by
        self.save(update_fields=[
            'approval_status', 'is_active', 'approval_decided_at', 'approved_by',
        ])

    def reject(self, by=None):
        """Reject the registration: keep the account locked out of login."""
        self.approval_status = self.APPROVAL_REJECTED
        self.is_active = False
        self.approval_decided_at = timezone.now()
        self.approved_by = by
        self.save(update_fields=[
            'approval_status', 'is_active', 'approval_decided_at', 'approved_by',
        ])


def generate_token_key() -> str:
    return secrets.token_urlsafe(32)


class ApiToken(models.Model):
    """A personal access token for the JSON API (``Authorization: Bearer <key>``).

    The key is stored as-is (like DRF's authtoken) and shown to the user only
    once, right after creation. Users manage their tokens on the profile page;
    the API itself is gated by the runtime setting ``api_enabled``.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_tokens',
    )
    name = models.CharField(_('Bezeichnung'), max_length=100)
    key = models.CharField(max_length=64, unique=True, default=generate_token_key,
                           editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(_('Zuletzt verwendet'), null=True, blank=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = _('API-Token')
        verbose_name_plural = _('API-Tokens')

    def __str__(self) -> str:
        return f'{self.name} ({self.user})'

    def touch(self) -> None:
        """Stamp last use without racing concurrent requests."""
        ApiToken.objects.filter(pk=self.pk).update(last_used_at=timezone.now())
