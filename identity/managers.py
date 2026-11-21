from django.contrib.auth.base_user import BaseUserManager


class UserManager(BaseUserManager):
    use_in_migrations = True

    def normalize_email(self, email: str | None) -> str | None:
        normalized = super().normalize_email(email)
        return normalized.strip().lower() or None

    def create_user(
        self,
        email: str | None = None,
        password: str | None = None,
        *,
        login_phone_number: str | None = None,
        **extra_fields,
    ):
        email = self.normalize_email(email)
        user = self.model(email=email, login_phone_number=login_phone_number, **extra_fields)
        user._normalize_identifiers()
        if not user.email and not user.login_phone_number:
            raise ValueError("Email or login phone number is required")
        user.set_password(password)
        user.full_clean(exclude={"password"})
        user.save(using=self._db)
        return user

    def create_superuser(self, email: str, password: str, **extra_fields):
        from identity.models import AccountStatus

        if not self.normalize_email(email):
            raise ValueError("Email is required")
        extra_fields.setdefault("name", "Administrator")
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("status", AccountStatus.ACTIVE)
        if not extra_fields["is_staff"] or not extra_fields["is_superuser"]:
            raise ValueError("A superuser must have is_staff and is_superuser enabled")
        return self.create_user(email, password, **extra_fields)
