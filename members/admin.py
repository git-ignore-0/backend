from django.contrib import admin

from members.models import MemberProfile


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "phone_number", "province_code", "ward_code", "updated_at")
    search_fields = ("user__email", "user__name", "phone_number", "province_code", "ward_code")
    readonly_fields = ("user", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False
