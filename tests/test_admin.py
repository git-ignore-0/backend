import pytest
from django.contrib import admin
from django.test import RequestFactory

from identity.admin import UserAdmin
from identity.models import AccountStatus, AuditLog, IdentityEvent, User


@pytest.mark.django_db
def test_identity_admin_cannot_create_or_delete_users(active_user):
    request = RequestFactory().get("/admin/identity/user/")
    request.user = active_user
    user_admin = UserAdmin(User, admin.site)

    assert user_admin.has_add_permission(request) is False
    assert user_admin.has_delete_permission(request, active_user) is False


def test_identity_admin_keeps_lifecycle_fields_read_only():
    user_admin = UserAdmin(User, admin.site)
    readonly = set(user_admin.readonly_fields)

    assert {"email", "status", "is_active"}.issubset(readonly)


@pytest.mark.django_db
def test_suspend_action_uses_lifecycle_service(active_user, monkeypatch):
    operator = User.objects.create_superuser(
        email="operator@example.com",
        name="Operator",
        password="StrongPassword12",
    )
    request = RequestFactory().post("/admin/identity/user/")
    request.user = operator
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)

    user_admin.suspend_accounts(request, User.objects.filter(pk=active_user.pk))

    active_user.refresh_from_db()
    assert active_user.status == AccountStatus.SUSPENDED
    assert active_user.is_active is False
    assert IdentityEvent.objects.filter(
        user=active_user,
        type="user.suspended",
        status="pending",
    ).exists()
    assert AuditLog.objects.filter(
        user=active_user,
        action="admin.account.suspended",
        context={"actor_user_id": str(operator.pk)},
    ).exists()


@pytest.mark.django_db
def test_deletion_action_requires_confirmation(active_user, monkeypatch):
    operator = User.objects.create_superuser(
        email="operator@example.com",
        name="Operator",
        password="StrongPassword12",
    )
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)

    preview_request = RequestFactory().post("/admin/identity/user/")
    preview_request.user = operator
    response = user_admin.request_account_deletion(
        preview_request,
        User.objects.filter(pk=active_user.pk),
    )
    assert response.template_name == "admin/identity/user/confirm_lifecycle_action.html"
    active_user.refresh_from_db()
    assert active_user.status == AccountStatus.ACTIVE

    confirm_request = RequestFactory().post(
        "/admin/identity/user/",
        {"confirm_lifecycle_action": "yes"},
    )
    confirm_request.user = operator
    user_admin.request_account_deletion(
        confirm_request,
        User.objects.filter(pk=active_user.pk),
    )

    active_user.refresh_from_db()
    assert active_user.status == AccountStatus.DELETION_REQUESTED
    assert IdentityEvent.objects.filter(user=active_user, type="user.deletion_requested").exists()


@pytest.mark.django_db
def test_lifecycle_admin_actions_skip_superusers(monkeypatch):
    operator = User.objects.create_superuser(
        email="operator@example.com",
        name="Operator",
        password="StrongPassword12",
    )
    request = RequestFactory().post("/admin/identity/user/")
    request.user = operator
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)

    user_admin.suspend_accounts(request, User.objects.filter(pk=operator.pk))

    operator.refresh_from_db()
    assert operator.status != AccountStatus.SUSPENDED
    assert IdentityEvent.objects.filter(user=operator, type="user.suspended").exists() is False
