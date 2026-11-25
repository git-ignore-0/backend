from django.contrib import admin
from django.db.models import Q
from django.urls import reverse
from django.utils.html import format_html
from django.utils.text import smart_split, unescape_string_literal
from django.utils.translation import gettext_lazy as _

from config.phones import format_vietnam_phone, phone_search
from members.models import MemberProfile


@admin.register(MemberProfile)
class MemberProfileAdmin(admin.ModelAdmin):
    list_display = (
        "display_user",
        "display_phone_number",
        "province_code",
        "ward_code",
        "updated_at",
    )
    list_select_related = ("user",)
    search_fields = ("user__email", "user__name", "province_code", "ward_code")
    exclude = ("user",)
    readonly_fields = ("display_user_link", "created_at", "updated_at")

    def has_add_permission(self, request):
        return False

    @admin.display(description=_("User"), ordering="user")
    def display_user(self, profile):
        user = profile.user
        return user.email or user.login_phone_display or str(user.pk)

    @admin.display(description=_("User"))
    def display_user_link(self, profile):
        url = reverse(
            "admin:identity_user_change", args=[profile.user_id], current_app=self.admin_site.name
        )
        return format_html('<a href="{}">{}</a>', url, self.display_user(profile))

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
