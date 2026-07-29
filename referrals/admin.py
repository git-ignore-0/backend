from django.contrib import admin

from referrals.models import PointTransaction, Referral, ReferralAccount


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return request.method in {"GET", "HEAD", "OPTIONS"}

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ReferralAccount)
class ReferralAccountAdmin(ReadOnlyAdmin):
    list_display = ("user", "code", "created_at")
    search_fields = ("user__email", "code")


@admin.register(Referral)
class ReferralAdmin(ReadOnlyAdmin):
    list_display = ("referrer", "referee", "created_at")
    search_fields = ("referrer__email", "referee__email")


@admin.register(PointTransaction)
class PointTransactionAdmin(ReadOnlyAdmin):
    list_display = ("user", "direction", "amount", "template", "actor", "created_at")
    list_filter = ("direction", "template")
    search_fields = ("user__email", "actor__email")
