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
    FARMS = "farms", "See our farms"
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
        return self.effective_status_at(timezone.now())

    def effective_status_at(self, instant):
        if self.start_at > instant:
            return "upcoming"
        if self.end_at <= instant:
            return "completed"
        return "ongoing"


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


class TestimonialType(models.TextChoices):
    CUSTOMER = "customer", "Customer"
    FARMER = "farmer", "Farmer"


class TestimonialStatus(models.TextChoices):
    DRAFT = "draft", "Draft"
    PUBLISHED = "published", "Published"
    HIDDEN = "hidden", "Hidden"


class Testimonial(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    type = models.CharField(max_length=16, choices=TestimonialType.choices)
    status = models.CharField(
        max_length=16,
        choices=TestimonialStatus.choices,
        default=TestimonialStatus.DRAFT,
    )
    display_name = models.CharField(max_length=240)
    image = models.ForeignKey(
        MediaAsset,
        related_name="testimonials",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    featured = models.BooleanField(default=False)
    sort_order = models.PositiveIntegerField(default=0)
    published_at = models.DateTimeField(null=True, blank=True)
    consent_confirmed = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="testimonials_created",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="testimonials_updated",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "-published_at", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["status", "type", "sort_order"],
                name="content_test_status_type_idx",
            ),
            models.Index(
                fields=["status", "sort_order"],
                name="content_test_status_order_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(type__in=TestimonialType.values),
                name="content_testimonial_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(status__in=TestimonialStatus.values),
                name="content_testimonial_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(status=TestimonialStatus.PUBLISHED)
                    | Q(consent_confirmed=True, published_at__isnull=False)
                ),
                name="content_published_testimonial_has_consent_and_time",
            ),
        ]

    def __str__(self):
        return self.display_name

    def clean(self):
        if self.status == TestimonialStatus.PUBLISHED:
            errors = {}
            if not self.consent_confirmed:
                errors["consent_confirmed"] = "Consent is required before publishing."
            if self.published_at is None:
                errors["published_at"] = "Published time is required for published testimonials."
            if errors:
                raise ValidationError(errors)


class TestimonialTranslation(models.Model):
    testimonial = models.ForeignKey(
        Testimonial,
        related_name="translations",
        on_delete=models.CASCADE,
    )
    locale = models.CharField(max_length=2, choices=[("vi", "Vietnamese"), ("en", "English")])
    role = models.CharField(max_length=240, blank=True)
    location = models.CharField(max_length=240, blank=True)
    quote = models.CharField(max_length=800, blank=True)
    full_story = models.TextField(max_length=20_000, blank=True)

    class Meta:
        ordering = ["locale", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["testimonial", "locale"],
                name="content_testimonial_translation_locale_unique",
            ),
            models.CheckConstraint(
                condition=Q(locale__in=sorted(SUPPORTED_LOCALES)),
                name="content_testimonial_translation_locale_supported",
            ),
        ]

    def __str__(self):
        return f"{self.testimonial_id}:{self.locale}"

    def clean(self):
        if self.locale not in SUPPORTED_LOCALES:
            raise ValidationError({"locale": "Unsupported locale."})


class TrackerFarm(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name_en = models.CharField(max_length=240)
    name_vi = models.CharField(max_length=240)
    location_en = models.CharField(max_length=240)
    location_vi = models.CharField(max_length=240)
    description_en = models.TextField(blank=True, default="")
    description_vi = models.TextField(blank=True, default="")
    image = models.ForeignKey(
        MediaAsset,
        related_name="tracker_farms",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
    )
    signup_count = models.PositiveIntegerField(default=0)
    is_visible = models.BooleanField(default=True)
    sort_order = models.PositiveIntegerField(default=0)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["sort_order", "created_at", "id"]
        indexes = [
            models.Index(
                fields=["is_visible", "sort_order", "created_at"],
                name="content_tracker_public_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(signup_count__gte=0),
                name="content_tracker_signup_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(sort_order__gte=0),
                name="content_tracker_sort_nonnegative",
            ),
        ]

    def __str__(self):
        return self.name_en


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
