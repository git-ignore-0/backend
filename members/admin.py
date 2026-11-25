from django.contrib import admin
from django.db.models import Q
from django.utils.text import smart_split, unescape_string_literal
from django.utils.translation import gettext_lazy as _

from config.phones import format_vietnam_phone, phone_search
from members.models import MemberProfile


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    list_display = ("user", "display_phone_number", "province_code", "ward_code", "updated_at")
    search_fields = ("user__email", "user__name", "province_code", "ward_code")
    readonly_fields = ("user", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("Phone number"), ordering="phone_number")
    def display_phone_number(self, profile):
        return format_vietnam_phone(profile.phone_number)

    def get_search_results(self, request, queryset, search_term):
        search_fields = self.get_search_fields(request)
        for term in smart_split(search_term):
            if term.startswith(('"', "'")) and term[0] == term[-1]:
                term = unescape_string_literal(term)
            text_matches = Q.create(
                [(f"{field}__icontains", term) for field in search_fields], connector=Q.OR
            )
            queryset = queryset.filter(
                text_matches | phone_search(term, "phone_number", legacy_presentation=True)
            )
        return queryset, False
