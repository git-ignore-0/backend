from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.template.response import TemplateResponse
from django.utils.translation import gettext_lazy as _

from identity.models import AccountStatus, AuditLog, IdentityEvent, User
from identity.services import InvalidAccountTransition, transition_account


class CoreUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    form = CoreUserChangeForm
    model = User
    ordering = ("-date_joined",)
    list_display = ("email", "name", "status", "is_staff", "date_joined")
    list_filter = ("status", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "name")
    readonly_fields = (
        "id",
        "email",
        "status",
        "is_active",
        "last_login",
        "date_joined",
        "updated_at",
    )
    fieldsets = (
        (None, {"fields": ("id", "email", "password")}),
        ("Profile", {"fields": ("name", "locale", "status")}),
        (
            "Permissions",
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        ("Timestamps", {"fields": ("last_login", "date_joined", "updated_at")}),
    )
    actions = ("suspend_accounts", "reactivate_accounts", "request_account_deletion")

    def _transition_accounts(self, request, queryset, *, target, allowed):
        changed = 0
        skipped = 0
        for user in queryset:
            if user.is_superuser or user.pk == request.user.pk or user.status not in allowed:
                skipped += 1
                continue
            try:
                transition_account(user, target)
            except InvalidAccountTransition:
                skipped += 1
            else:
                AuditLog.objects.create(
                    user=user,
                    action=f"admin.account.{target}",
                    ip_address=request.META.get("REMOTE_ADDR"),
                    user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
                    context={"actor_user_id": str(request.user.pk)},
                )
                changed += 1

        self.message_user(
            request,
            _("%(changed)d account(s) updated; %(skipped)d skipped.")
            % {"changed": changed, "skipped": skipped},
            messages.SUCCESS if changed else messages.WARNING,
        )

    @admin.action(description=_("Suspend selected accounts"))
    def suspend_accounts(self, request, queryset):
        self._transition_accounts(
            request,
            queryset,
            target=AccountStatus.SUSPENDED,
            allowed={AccountStatus.ACTIVE},
        )

    @admin.action(description=_("Reactivate selected suspended accounts"))
    def reactivate_accounts(self, request, queryset):
        self._transition_accounts(
            request,
            queryset,
            target=AccountStatus.ACTIVE,
            allowed={AccountStatus.SUSPENDED},
        )

    @admin.action(description=_("Request deletion and hide forum content"))
    def request_account_deletion(self, request, queryset):
        if request.POST.get("confirm_lifecycle_action") == "yes":
            self._transition_accounts(
                request,
                queryset,
                target=AccountStatus.DELETION_REQUESTED,
                allowed={AccountStatus.ACTIVE, AccountStatus.SUSPENDED},
            )
            return None

        context = {
            **self.admin_site.each_context(request),
            "title": _("Confirm account deletion request"),
            "queryset": queryset,
            "opts": self.model._meta,
            "action_checkbox_name": ACTION_CHECKBOX_NAME,
            "action_name": "request_account_deletion",
        }
        return TemplateResponse(
            request,
            "admin/identity/user/confirm_lifecycle_action.html",
            context,
        )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(IdentityEvent)
class IdentityEventAdmin(admin.ModelAdmin):
    list_display = ("type", "user", "status", "attempts", "created_at")
    list_filter = ("status", "type")
    search_fields = ("user__email", "type")
    readonly_fields = [field.name for field in IdentityEvent._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "ip_address", "created_at")
    list_filter = ("action",)
    search_fields = ("user__email", "action")
    readonly_fields = [field.name for field in AuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
