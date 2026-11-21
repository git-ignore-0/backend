import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from identity.managers import UserManager
from identity.phones import normalize_login_phone


class AccountStatus(models.TextChoices):
    PENDING_ACTIVATION = "pending_activation", "Pending activation"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, max_length=254, null=True, blank=True)
    email_verification_attempted_at = models.DateTimeField(null=True, blank=True, editable=False)
    login_phone_number = models.CharField(max_length=16, unique=True, null=True, blank=True)
    name = models.CharField(max_length=150)
    status = models.CharField(
        max_length=32,
        choices=AccountStatus.choices,
        default=AccountStatus.PENDING_ACTIVATION,
        db_index=True,
    )
    locale = models.CharField(max_length=10, default="vi")
    is_staff = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    date_joined = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["name"]

    class Meta:
        ordering = ["-date_joined"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        status__in=[
                            AccountStatus.PENDING_ACTIVATION,
                            AccountStatus.ACTIVE,
                        ],
                        is_active=True,
                    )
                    | Q(
                        status__in=[
                            AccountStatus.SUSPENDED,
                            AccountStatus.DELETION_REQUESTED,
                            AccountStatus.ANONYMIZED,
                        ],
                        is_active=False,
                    )
                ),
                name="identity_user_status_matches_is_active",
            )
        ]

    def _normalize_identifiers(self):
        self.email = User.objects.normalize_email(self.email)
        if isinstance(self.login_phone_number, str) and not self.login_phone_number.strip(" "):
            self.login_phone_number = None
        if self.login_phone_number is not None:
            self.login_phone_number = normalize_login_phone(self.login_phone_number)

    def clean_fields(self, exclude=None):
        self._normalize_identifiers()
        super().clean_fields(exclude=exclude)

    def clean(self):
        # AbstractBaseUser.clean applies username NFKC; email uses the same
        # trim/lower contract as manager, save and migration instead.
        self._normalize_identifiers()

    def save(self, *args, **kwargs):
        # Model/manager/service saves share the same canonical identifiers.
        self._normalize_identifiers()
        return super().save(*args, **kwargs)

    def __str__(self) -> str:
        return self.email or self.login_phone_number or str(self.pk)

    @property
    def email_verified(self) -> bool:
        if not self.email:
            return False
        # Database collations may equate distinct accented addresses. Verification
        # requires the actual canonical string, not SQL collation equivalence.
        addresses = getattr(self, "verified_addresses", None)
        emails = (
            (address.email for address in addresses)
            if addresses is not None
            else self.emailaddress_set.filter(verified=True).values_list("email", flat=True)
        )
        return any(User.objects.normalize_email(email) == self.email for email in emails)

    def can_authenticate(self) -> bool:
        return self.is_active and self.status in {
            AccountStatus.PENDING_ACTIVATION,
            AccountStatus.ACTIVE,
        }

    def can_access_core(self, *, email_verified: bool | None = None) -> bool:
        # Retain the legacy keyword while separating Core access from verification.
        return self.is_active and self.status == AccountStatus.ACTIVE


class EventStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    PROCESSING = "processing", "Processing"
    PROCESSED = "processed", "Processed"
    FAILED = "failed", "Failed"


class IdentityEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    type = models.CharField(max_length=100, db_index=True)
    payload = models.JSONField(default=dict)
    status = models.CharField(
        max_length=20,
        choices=EventStatus.choices,
        default=EventStatus.PENDING,
        db_index=True,
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    available_at = models.DateTimeField(null=True, blank=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self) -> str:
        return f"{self.type} ({self.status})"


class AuditLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=100, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.TextField(blank=True)
    context = models.JSONField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} at {self.created_at.isoformat()}"
