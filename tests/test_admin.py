import pytest
from django.contrib import admin
from django.test import RequestFactory

from identity.admin import UserAdmin
from identity.models import User


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
