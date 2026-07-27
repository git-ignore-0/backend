from django.conf import settings
from django.db import models


class MemberProfile(models.Model):
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        primary_key=True,
        related_name="member_profile",
        on_delete=models.CASCADE,
    )
    phone_number = models.CharField(max_length=32, blank=True)
    date_of_birth = models.DateField(null=True, blank=True)
    province_code = models.CharField(max_length=2, blank=True)
    ward_code = models.CharField(max_length=5, blank=True)
    address = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Member profile for {self.user_id}"
