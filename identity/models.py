import uuid

from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db import models
from django.db.models import Q
from django.utils import timezone

from identity.managers import UserManager


class AccountStatus(models.TextChoices):
    PENDING_VERIFICATION = "pending_verification", "Pending verification"
    ACTIVE = "active", "Active"
    SUSPENDED = "suspended", "Suspended"
    DELETION_REQUESTED = "deletion_requested", "Deletion requested"
    ANONYMIZED = "anonymized", "Anonymized"


class User(AbstractBaseUser, PermissionsMixin):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    email = models.EmailField(unique=True, max_length=254)
    name = models.CharField(max_length=150)
    status = models.CharField(
        max_length=32,
        choices=AccountStatus.choices,
        default=AccountStatus.PENDING_VERIFICATION,
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
                            AccountStatus.PENDING_VERIFICATION,
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

    def __str__(self) -> str:
        return self.email

    @property
    def email_verified(self) -> bool:
        return self.emailaddress_set.filter(email__iexact=self.email, verified=True).exists()

    def can_authenticate(self) -> bool:
        return self.is_active and self.status in {
            AccountStatus.PENDING_VERIFICATION,
            AccountStatus.ACTIVE,
        }

    def can_access_core(self, *, email_verified: bool | None = None) -> bool:
        if not self.is_active or self.status != AccountStatus.ACTIVE:
            return False
        if email_verified is None:
            email_verified = self.email_verified
        return email_verified


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
