from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy as _l

from .models import ApiToken, User


@admin.register(ApiToken)
class ApiTokenAdmin(admin.ModelAdmin):
    """Tokens are managed by users on their profile page; the admin only lists
    them (revoke = delete). The key itself is never displayed here."""

    list_display = ('name', 'user', 'created_at', 'last_used_at')
    readonly_fields = ('user', 'name', 'created_at', 'last_used_at')
    exclude = ('key',)

    def has_add_permission(self, request):
        return False


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    list_display = (
        'username', 'email', 'display_name', 'approval_status',
        'is_active', 'date_joined',
    )
    list_filter = UserAdmin.list_filter + ('approval_status',)
    ordering = ('approval_status', '-date_joined')
    readonly_fields = ('approval_decided_at', 'approved_by')
    actions = ('approve_users', 'reject_users')

    fieldsets = UserAdmin.fieldsets + (
        (_l('Profil'), {'fields': ('display_name',)}),
        (_l('Freigabe (Whitelist)'), {
            'fields': (
                'approval_status', 'approval_requested_at',
                'approval_decided_at', 'approved_by',
            ),
        }),
    )

    @admin.action(description=_l('Ausgewählte Konten freigeben (Whitelist)'))
    def approve_users(self, request, queryset):
        count = 0
        for user in queryset:
            user.approve(by=request.user)
            count += 1
        self.message_user(
            request, _('%(count)d Konto/Konten freigegeben.') % {'count': count}, messages.SUCCESS,
        )

    @admin.action(description=_l('Ausgewählte Registrierungen ablehnen'))
    def reject_users(self, request, queryset):
        count = 0
        for user in queryset:
            user.reject(by=request.user)
            count += 1
        self.message_user(
            request, _('%(count)d Registrierung(en) abgelehnt.') % {'count': count}, messages.WARNING,
        )
