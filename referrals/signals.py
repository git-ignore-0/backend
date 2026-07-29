from django.db.models.signals import post_save
from django.dispatch import receiver

from identity.models import User
from referrals.services import ensure_referral_account


@receiver(post_save, sender=User)
def create_referral_account(sender, instance, created, **kwargs) -> None:
    if created:
        ensure_referral_account(instance)
