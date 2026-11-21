from django.contrib import admin, messages
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.core.exceptions import PermissionDenied
from django.db import transaction
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

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            return

        # A lifecycle transition or password reset may have saved after the form load.
        changed_fields = {
            field.name
            for field in obj._meta.concrete_fields
            if field.editable and field.name in form.changed_data
        }
        if changed_fields:
            obj.save(update_fields=changed_fields | {"updated_at"})

    @staticmethod
    def _require_lifecycle_actor(request):
        # Refresh membership and privileges to bypass the request user's permission cache.
        actor = User.objects.filter(pk=request.user.pk).first()
        if (
            actor is None
            or not actor.is_staff
            or not actor.can_access_core()
            or not actor.has_perm("identity.change_user")
        ):
            raise PermissionDenied
        return actor

    def _transition_accounts(self, request, queryset, *, target, allowed):
        actor = self._require_lifecycle_actor(request)
        changed = 0
        skipped = 0
        for candidate in queryset:
            try:
                with transaction.atomic():
                    user = User.objects.select_for_update().get(pk=candidate.pk)
                    if user.is_superuser or user.pk == actor.pk or user.status not in allowed:
                        skipped += 1
                        continue
                    updated = transition_account(user, target, source="admin", actor=actor)
                    AuditLog.objects.create(
                        user=updated,
                        action=f"admin.account.{target}",
                        ip_address=request.META.get("REMOTE_ADDR"),
                        user_agent=request.META.get("HTTP_USER_AGENT", "")[:1000],
                        context={"actor_user_id": str(actor.pk)},
                    )
            except InvalidAccountTransition:
                skipped += 1
            else:
                changed += 1

        self.message_user(
            request,
            _("%(changed)d account(s) updated; %(skipped)d skipped.")
            % {"changed": changed, "skipped": skipped},
            messages.SUCCESS if changed else messages.WARNING,
        )

    @admin.action(permissions=["change"], description=_("Suspend selected accounts"))
    def suspend_accounts(self, request, queryset):
        self._transition_accounts(
            request,
            queryset,
            target=AccountStatus.SUSPENDED,
            allowed={AccountStatus.ACTIVE},
        )

    @admin.action(permissions=["change"], description=_("Reactivate selected suspended accounts"))
    def reactivate_accounts(self, request, queryset):
        self._transition_accounts(
            request,
            queryset,
            target=AccountStatus.ACTIVE,
            allowed={AccountStatus.SUSPENDED},
        )

    @admin.action(permissions=["change"], description=_("Request deletion and hide forum content"))
    def request_account_deletion(self, request, queryset):
        if request.POST.get("confirm_lifecycle_action") == "yes":
            self._transition_accounts(
                request,
                queryset,
                target=AccountStatus.DELETION_REQUESTED,
                allowed={AccountStatus.ACTIVE, AccountStatus.SUSPENDED},
            )
            return None

        self._require_lifecycle_actor(request)
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
