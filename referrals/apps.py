from django.apps import AppConfig


class ReferralsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "referrals"
    verbose_name = "Referrals and points"

    def ready(self) -> None:
        from referrals import signals  # noqa: F401
