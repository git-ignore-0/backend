import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import Q
from django.utils import timezone

SUPPORTED_LOCALES = {"en", "vi"}


class SiteSettings(models.Model):
    key = models.CharField(primary_key=True, max_length=32, default="default", editable=False)
    email = models.EmailField(blank=True)
    is_email_enabled = models.BooleanField(default=True)
    phone_display = models.CharField(max_length=50, blank=True)
    is_phone_enabled = models.BooleanField(default=True)
    version = models.PositiveIntegerField(default=1)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Site settings"

    def __str__(self):
        return "Natural Farming Vietnam site settings"


class ExternalLinkKind(models.TextChoices):
    FACEBOOK = "facebook", "Facebook"
    YOUTUBE = "youtube", "YouTube"
    STORE = "store", "Store"
    FORUM = "forum", "Forum"
    OTHER = "other", "Other"


class ExternalLink(models.Model):
    settings = models.ForeignKey(SiteSettings, related_name="links", on_delete=models.CASCADE)
    kind = models.CharField(max_length=32, choices=ExternalLinkKind.choices)
    label = models.CharField(max_length=100, blank=True)
    url = models.URLField(max_length=500, blank=True)
    position = models.PositiveSmallIntegerField(default=0)
    is_enabled = models.BooleanField(default=True)

    class Meta:
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["settings", "kind"],
                condition=~Q(kind=ExternalLinkKind.OTHER),
                name="content_unique_standard_external_link",
            )
        ]

    def __str__(self):
        return self.label or self.get_kind_display()


class MediaAsset(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ImageField(upload_to="workshops/%Y/%m")
    variants = models.JSONField(default=list)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    original_name = models.CharField(max_length=255)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name


class Workshop(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=180, unique=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        related_name="created_workshops",
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return self.slug


class WorkshopRevision(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workshop = models.ForeignKey(Workshop, related_name="revisions", on_delete=models.CASCADE)
    revision_number = models.PositiveIntegerField()
    is_draft = models.BooleanField(default=True)
    is_published = models.BooleanField(default=False)
    default_locale = models.CharField(max_length=10, default="vi")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    event_timezone = models.CharField(max_length=64, default="Asia/Ho_Chi_Minh")
    registration_url = models.URLField(max_length=500, blank=True)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    published_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-revision_number"]
        constraints = [
            models.UniqueConstraint(
                fields=["workshop", "revision_number"], name="content_unique_workshop_revision"
            ),
            models.UniqueConstraint(
                fields=["workshop"],
                condition=Q(is_draft=True),
                name="content_one_draft_revision_per_workshop",
            ),
            models.UniqueConstraint(
                fields=["workshop"],
                condition=Q(is_published=True),
                name="content_one_published_revision_per_workshop",
            ),
            models.CheckConstraint(
                condition=Q(end_at__gt=models.F("start_at")),
                name="content_workshop_end_after_start",
            ),
            models.CheckConstraint(
                condition=~Q(is_draft=True, is_published=True),
                name="content_workshop_revision_not_draft_and_published",
            ),
        ]

    def __str__(self):
        return f"{self.workshop.slug} · revision {self.revision_number}"

    def clean(self):
        if self.default_locale not in SUPPORTED_LOCALES:
            raise ValidationError({"default_locale": "Unsupported locale."})
        try:
            ZoneInfo(self.event_timezone)
        except ZoneInfoNotFoundError as error:
            raise ValidationError({"event_timezone": "Unknown IANA timezone."}) from error
        if self.end_at and self.start_at and self.end_at <= self.start_at:
            raise ValidationError({"end_at": "End time must be after start time."})

    @property
    def effective_status(self):
        if self.end_at <= timezone.now():
            return "completed"
        return "upcoming"


def empty_tiptap_document():
    return {"type": "doc", "content": [{"type": "paragraph"}]}


class WorkshopTranslation(models.Model):
    revision = models.ForeignKey(
        WorkshopRevision, related_name="translations", on_delete=models.CASCADE
    )
    locale = models.CharField(max_length=10)
    title = models.CharField(max_length=240, blank=True)
    summary = models.TextField(blank=True)
    body = models.JSONField(default=empty_tiptap_document)
    is_ready = models.BooleanField(default=False)

    class Meta:
        ordering = ["locale"]
        constraints = [
            models.UniqueConstraint(
                fields=["revision", "locale"], name="content_unique_workshop_translation_locale"
            )
        ]

    def __str__(self):
        return f"{self.title or self.revision.workshop.slug} ({self.locale})"

    def clean(self):
        if self.locale not in SUPPORTED_LOCALES:
            raise ValidationError({"locale": "Unsupported locale."})


class ContentAuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100, db_index=True)
    entity_type = models.CharField(max_length=50)
    entity_id = models.CharField(max_length=100)
    context = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} · {self.entity_type}:{self.entity_id}"
