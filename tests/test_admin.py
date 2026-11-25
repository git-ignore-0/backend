import pytest
from django.contrib import admin
from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME
from django.contrib.auth.models import Group, Permission
from django.test import RequestFactory
from django.urls import reverse

from identity.admin import UserAdmin
from identity.models import AccountStatus, AuditLog, IdentityEvent, User
from identity.services import transition_account
from members.admin import MemberProfileAdmin
from members.models import MemberProfile


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
def test_member_profile_admin_combines_phone_and_text_search_terms(rf):
    garden = User.objects.create_user(email="garden@example.com", name="Garden Member")
    orchard = User.objects.create_user(email="orchard@example.com", name="Orchard Member")
    MemberProfile.objects.create(user=garden, phone_number="+84 (98) 123-4567")
    MemberProfile.objects.create(user=orchard, phone_number="0904567890")
    profile_admin = MemberProfileAdmin(MemberProfile, admin.site)
    request = rf.get("/admin/members/memberprofile/")

    for term in ('"Garden Member" 4567', "0981234567", "098"):
        results, may_duplicate = profile_admin.get_search_results(
            request, MemberProfile.objects.all(), term
        )
        assert not may_duplicate
        assert list(results.values_list("user_id", flat=True)) == [garden.pk]

    results, _ = profile_admin.get_search_results(
        request, MemberProfile.objects.all(), "+84981234567"
    )
    assert not results.exists()


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


@pytest.mark.django_db
def test_reactivate_action_uses_admin_evidence_without_verifying_email(monkeypatch):
    operator = User.objects.create_superuser("operator@example.com", "secret")
    user = User.objects.create_user(
        name="Suspended", login_phone_number="0901234567", status="suspended", is_active=False
    )
    request = RequestFactory().post("/admin/identity/user/")
    request.user = operator
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)
    user_admin.reactivate_accounts(request, User.objects.filter(pk=user.pk))
    user.refresh_from_db()
    assert user.status == "active"
    assert not user.email_verified
    assert IdentityEvent.objects.filter(user=user, type="user.activated").count() == 1
    assert AuditLog.objects.filter(user=user, action="user.activated").count() == 1
    assert AuditLog.objects.get(user=user, action="admin.account.active").context == {
        "actor_user_id": str(operator.pk),
    }


@pytest.mark.django_db
def test_admin_audit_failure_rolls_back_lifecycle(active_user, monkeypatch):
    from django.db import IntegrityError

    operator = User.objects.create_superuser("operator@example.com", "secret")
    request = RequestFactory().post("/admin/identity/user/")
    request.user = operator
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)

    def fail(**kwargs):
        raise IntegrityError("audit failed")

    monkeypatch.setattr(AuditLog.objects, "create", fail)
    with pytest.raises(IntegrityError):
        user_admin.suspend_accounts(request, User.objects.filter(pk=active_user.pk))
    active_user.refresh_from_db()
    assert active_user.status == "active"
    assert not IdentityEvent.objects.filter(user=active_user, type="user.suspended").exists()


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["suspend", "promote_superuser"])
def test_admin_rechecks_protected_target_and_state_after_lock(active_user, monkeypatch, change):
    operator = User.objects.create_superuser("operator@example.com", "secret")
    request = RequestFactory().post("/admin/identity/user/")
    request.user = operator
    user_admin = UserAdmin(User, admin.site)
    monkeypatch.setattr(user_admin, "message_user", lambda *args, **kwargs: None)
    stale = User.objects.filter(pk=active_user.pk)
    list(stale)
    if change == "suspend":
        transition_account(active_user, "suspended")
    else:
        User.objects.filter(pk=active_user.pk).update(is_superuser=True)
    event_count = IdentityEvent.objects.filter(user=active_user).count()
    user_admin.suspend_accounts(request, stale)
    active_user.refresh_from_db()
    assert active_user.status == ("suspended" if change == "suspend" else "active")
    assert IdentityEvent.objects.filter(user=active_user).count() == event_count
    assert not AuditLog.objects.filter(user=active_user, action="admin.account.suspended").exists()


@pytest.fixture
def staff_operator(db):
    operator = User.objects.create_user(
        "staff@example.com", name="Staff", is_staff=True, status=AccountStatus.ACTIVE
    )
    group, _ = Group.objects.get_or_create(name="website_admin")
    operator.groups.add(group)
    operator.user_permissions.add(
        Permission.objects.get(content_type__app_label="identity", codename="view_user")
    )
    return operator


@pytest.mark.parametrize("can_change", [False, True])
@pytest.mark.parametrize(
    ("action", "initial_status", "target_status", "event_type", "audit_action"),
    [
        (
            "suspend_accounts",
            AccountStatus.ACTIVE,
            AccountStatus.SUSPENDED,
            "user.suspended",
            "admin.account.suspended",
        ),
        (
            "reactivate_accounts",
            AccountStatus.SUSPENDED,
            AccountStatus.ACTIVE,
            "user.activated",
            "admin.account.active",
        ),
        (
            "request_account_deletion",
            AccountStatus.ACTIVE,
            AccountStatus.DELETION_REQUESTED,
            "user.deletion_requested",
            "admin.account.deletion_requested",
        ),
    ],
)
def test_admin_lifecycle_action_requires_change_permission(
    client,
    active_user,
    staff_operator,
    can_change,
    action,
    initial_status,
    target_status,
    event_type,
    audit_action,
):
    if initial_status == AccountStatus.SUSPENDED:
        transition_account(active_user, initial_status)
    if can_change:
        staff_operator.user_permissions.add(
            Permission.objects.get(content_type__app_label="identity", codename="change_user")
        )
    client.force_login(staff_operator)
    url = reverse("admin:identity_user_changelist")
    changelist = client.get(url)
    assert changelist.status_code == 200

    data = {"action": action, ACTION_CHECKBOX_NAME: str(active_user.pk), "index": 0}
    if action == "request_account_deletion":
        preview = client.post(url, data)
        assert (preview.status_code == 200 and "queryset" in preview.context_data) == can_change
        assert User.objects.get(pk=active_user.pk).status == initial_status
        data.pop("index")
        data["confirm_lifecycle_action"] = "yes"
    response = client.post(url, data)

    assert response.status_code == (302 if can_change else 200)
    active_user.refresh_from_db()
    assert active_user.status == (target_status if can_change else initial_status)
    assert IdentityEvent.objects.filter(user=active_user, type=event_type).count() == int(
        can_change
    )
    assert AuditLog.objects.filter(user=active_user, action=audit_action).count() == int(can_change)
    assert (f'value="{action}"'.encode() in changelist.content) == can_change


@pytest.mark.parametrize("revoked", ["staff", "active", "change_permission"])
def test_admin_lifecycle_rechecks_current_operator(
    client, active_user, staff_operator, monkeypatch, revoked
):
    change = Permission.objects.get(content_type__app_label="identity", codename="change_user")
    staff_operator.user_permissions.add(change)
    client.force_login(staff_operator)
    response_action = UserAdmin.response_action

    def respond_after_revocation(self, request, queryset):
        assert self.has_change_permission(request)
        if revoked == "staff":
            User.objects.filter(pk=staff_operator.pk).update(is_staff=False)
        elif revoked == "active":
            transition_account(staff_operator, AccountStatus.SUSPENDED)
        else:
            staff_operator.user_permissions.remove(change)
        return response_action(self, request, queryset)

    monkeypatch.setattr(UserAdmin, "response_action", respond_after_revocation)
    response = client.post(
        reverse("admin:identity_user_changelist"),
        {"action": "suspend_accounts", ACTION_CHECKBOX_NAME: str(active_user.pk), "index": 0},
    )

    assert response.status_code == 403
    active_user.refresh_from_db()
    assert active_user.status == AccountStatus.ACTIVE
    assert not IdentityEvent.objects.filter(user=active_user, type="user.suspended").exists()


@pytest.mark.parametrize("change_name", [False, True])
def test_admin_change_form_preserves_concurrent_identity_updates(
    client,
    active_user,
    monkeypatch,
    change_name,
):
    operator = User.objects.create_superuser("operator@example.com", "secret")
    client.force_login(operator)
    group = Group.objects.create(name="Member Group")
    permission = Permission.objects.get(content_type__app_label="identity", codename="view_user")
    save_model = UserAdmin.save_model

    def save_after_identity_change(self, request, obj, form, change):
        concurrent_user = transition_account(active_user, AccountStatus.SUSPENDED)
        concurrent_user.set_password("NewPassword12345")
        concurrent_user.save(update_fields=["password"])
        return save_model(self, request, obj, form, change)

    monkeypatch.setattr(UserAdmin, "save_model", save_after_identity_change)
    name = "Updated name" if change_name else active_user.name
    response = client.post(
        reverse("admin:identity_user_change", args=[active_user.pk]),
        {
            "name": name,
            "locale": active_user.locale,
            "groups": [group.pk],
            "user_permissions": [permission.pk],
            "_save": "Save",
        },
    )

    assert response.status_code == 302
    active_user.refresh_from_db()
    assert active_user.name == name
    assert active_user.status == AccountStatus.SUSPENDED
    assert not active_user.is_active
    assert active_user.check_password("NewPassword12345")
    assert list(active_user.groups.all()) == [group]
    assert list(active_user.user_permissions.all()) == [permission]
