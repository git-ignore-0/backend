from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.contrib.auth.forms import UserChangeForm, UserCreationForm

from identity.models import AccountStatus, AuditLog, IdentityEvent, User


class CoreUserCreationForm(UserCreationForm):
    class Meta:
        model = User
        fields = ("email", "name", "locale")


class CoreUserChangeForm(UserChangeForm):
    class Meta:
        model = User
        fields = "__all__"


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    add_form = CoreUserCreationForm
    form = CoreUserChangeForm
    model = User
    ordering = ("-date_joined",)
    list_display = ("email", "name", "status", "is_staff", "date_joined")
    list_filter = ("status", "is_staff", "is_superuser", "groups")
    search_fields = ("email", "name")
    readonly_fields = ("id", "last_login", "date_joined", "updated_at")
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
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "name", "locale", "password1", "password2"),
            },
        ),
    )
    actions = None

    def has_delete_permission(self, request, obj=None):
        return False

    def save_model(self, request, obj, form, change) -> None:
        obj.email = obj.email.strip().lower()
        obj.is_active = obj.status in {
            AccountStatus.PENDING_VERIFICATION,
            AccountStatus.ACTIVE,
        }
        super().save_model(request, obj, form, change)


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
