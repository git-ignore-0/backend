from django.contrib import admin

from content.models import ContentAuditLog, MediaAsset, SiteSettings, Workshop


@admin.register(SiteSettings)
class SiteSettingsAdmin(admin.ModelAdmin):
    list_display = ("key", "email", "phone_display", "version", "updated_at")
    readonly_fields = ("version", "updated_by", "updated_at")


@admin.register(Workshop)
class WorkshopAdmin(admin.ModelAdmin):
    list_display = ("slug", "created_at", "updated_at", "archived_at")
    search_fields = ("slug", "revisions__translations__title")
    readonly_fields = ("created_by", "created_at", "updated_at")


@admin.register(MediaAsset)
class MediaAssetAdmin(admin.ModelAdmin):
    list_display = ("original_name", "width", "height", "created_by", "created_at")
    readonly_fields = [field.name for field in MediaAsset._meta.fields]


@admin.register(ContentAuditLog)
class ContentAuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "entity_type", "entity_id", "actor", "created_at")
    list_filter = ("action", "entity_type")
    readonly_fields = [field.name for field in ContentAuditLog._meta.fields]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
