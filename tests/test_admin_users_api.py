import pytest
from allauth.account.models import EmailAddress
from django.contrib.auth.models import Group
from django.utils import timezone
from rest_framework.test import APIClient

from identity.models import AuditLog, IdentityEvent, User

pytestmark = pytest.mark.django_db
USERS = "/api/v1/admin/users"
PASSWORD = "  Secure-Harvest-42!  "


@pytest.fixture
def cms(active_user, access_token):
    active_user.groups.add(Group.objects.get_or_create(name="website_admin")[0])
    access_token.scope = "profile email content:manage"
    access_token.save(update_fields=["scope"])
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {access_token.token}")
    return client


def payload(**fields):
    return {
        "name": "  Garden Member  ",
        "login_phone_number": "090 123 4567",
        "password1": PASSWORD,
        "password2": PASSWORD,
        **fields,
    }


def target(status="pending_activation", **fields):
    return User.objects.create_user(
        password="Previous-password-42!",
        name="Garden Member",
        login_phone_number="0912345678",
        status=status,
        is_active=status in {"pending_activation", "active"},
        **fields,
    )


@pytest.mark.parametrize("email", [None, "", "  ", "  PERSON@Example.com  "])
def test_create_and_activate_then_login(cms, active_user, client, mailoutbox, email):
    response = cms.post(USERS, payload(email=email), format="json")
    assert response.status_code == 201
    data = response.json()["data"]
    assert set(data) == {
        "id",
        "name",
        "login_phone_number",
        "email",
        "email_verified",
        "status",
        "locale",
        "date_joined",
    }
    user = User.objects.get(pk=data["id"])
    assert data["name"] == "Garden Member"
    assert data["login_phone_number"] == "+84901234567"
    assert data["locale"] == "vi"
    assert user.status == "pending_activation" and user.is_active
    assert not user.is_staff and not user.is_superuser and not user.groups.exists()
    assert user.check_password(PASSWORD) and not user.check_password(PASSWORD.strip())
    assert data["email"] == ("person@example.com" if email and email.strip() else None)
    addresses = EmailAddress.objects.filter(user=user)
    assert addresses.count() == bool(user.email)
    if user.email:
        assert addresses.get().primary and not addresses.get().verified
    assert not data["email_verified"]
    assert user.referral_account.code
    audit = AuditLog.objects.get(user=user, action="user.created")
    assert audit.context == {"actor_user_id": str(active_user.pk)}
    activated = cms.post(f"{USERS}/{user.pk}/activate", {}, format="json")
    assert activated.status_code == 200 and not activated.json()["data"]["email_verified"]
    signed_in = client.post("/login", {"login": "0901234567", "password": PASSWORD})
    assert signed_in.status_code == 302 and signed_in.url == "/account"
    assert client.get("/account").status_code == 200
    assert not mailoutbox


@pytest.mark.parametrize(
    "fields",
    [
        {"name": " "},
        {"name": "x" * 151},
        {"login_phone_number": "123"},
        {"login_phone_number": None},
        {"locale": "fr"},
        {"email": "bad"},
        {"email": "member@" + ".".join(["İ" * 50] * 3) + ".com"},
        {"password2": "mismatch"},
        {"password1": "short", "password2": "short"},
        {
            "email": "gardening-member@example.com",
            "password1": "gardening-member",
            "password2": "gardening-member",
        },
        {"is_superuser": True},
        {"email_verified": True},
        {"status": "active"},
        {"roles": ["admin"]},
    ],
)
def test_create_rejects_invalid_fields(cms, fields):
    before = User.objects.count()
    assert cms.post(USERS, payload(**fields), format="json").status_code == 400
    assert User.objects.count() == before


@pytest.mark.parametrize(
    "duplicate",
    [
        {"login_phone_number": "84901234567"},
        {"login_phone_number": "0912345678", "email": "PERSON@example.com"},
    ],
)
def test_create_rejects_duplicate_identity(cms, duplicate):
    assert cms.post(USERS, payload(email="person@example.com"), format="json").status_code == 201
    assert cms.post(USERS, payload(**duplicate), format="json").status_code == 400


@pytest.mark.parametrize(
    ("action", "start", "end"),
    [
        ("activate", "pending_activation", "active"),
        ("activate", "active", "active"),
        ("activate", "suspended", None),
        ("suspend", "active", "suspended"),
        ("suspend", "suspended", "suspended"),
        ("suspend", "pending_activation", None),
        ("reactivate", "suspended", "active"),
        ("reactivate", "active", "active"),
        ("reactivate", "pending_activation", None),
        *[
            (action, state, None)
            for action in ("activate", "suspend", "reactivate")
            for state in ("deletion_requested", "anonymized")
        ],
    ],
)
def test_state_matrix_and_retry(cms, active_user, action, start, end):
    user = target(start)
    url = f"{USERS}/{user.pk}/{action}"
    for _ in range(2):
        result = cms.post(url)
        assert result.status_code == (200 if end else 409)
        if not end:
            assert result.json()["errors"][0]["code"] == "account_state_conflict"
    user.refresh_from_db()
    assert user.status == (end or start) and not user.email_verified
    changed = bool(end and end != start)
    assert IdentityEvent.objects.filter(user=user).count() == changed
    entries = AuditLog.objects.filter(user=user)
    assert entries.count() == changed
    if changed:
        assert entries.get().context["actor_user_id"] == str(active_user.pk)


@pytest.mark.parametrize(
    "mode", ["missing", "invalid", "session", "scope", "group", "superuser", "locked"]
)
def test_mutation_requires_bearer_group_scope_and_active_actor(
    cms, active_user, access_token, mode
):
    expected = 401
    if mode in {"missing", "session"}:
        cms.credentials()
        if mode == "session":
            cms.force_login(active_user)
    elif mode == "invalid":
        cms.credentials(HTTP_AUTHORIZATION="Bearer invalid")
    elif mode == "locked":
        User.objects.filter(pk=active_user.pk).update(status="suspended", is_active=False)
    elif mode == "scope":
        access_token.scope = "profile email"
        access_token.save(update_fields=["scope"])
        expected = 403
    else:
        active_user.groups.clear()
        if mode == "superuser":
            User.objects.filter(pk=active_user.pk).update(is_superuser=True)
        expected = 403
    user = target("active")
    assert cms.post(USERS, payload(), format="json").status_code == expected
    assert cms.post(f"{USERS}/{user.pk}/suspend").status_code == expected


@pytest.mark.parametrize("action", ["suspend", "reset-password"])
@pytest.mark.parametrize("protected", ["self", "superuser"])
def test_protected_target(cms, active_user, action, protected):
    user = active_user if protected == "self" else target("suspended", is_superuser=True)
    body = {"password1": PASSWORD, "password2": PASSWORD} if action == "reset-password" else {}
    result = cms.post(f"{USERS}/{user.pk}/{action}", body, format="json")
    assert result.status_code == 409
    assert result.json()["errors"][0]["code"] == "account_target_protected"
    assert not AuditLog.objects.filter(user=user).exists()


@pytest.mark.parametrize(
    "state", ["active", "pending_activation", "suspended", "deletion_requested", "anonymized"]
)
@pytest.mark.parametrize("evidence", ["missing", "unverified", "verified"])
def test_reset_eligibility_preserves_status(cms, mailoutbox, state, evidence):
    email = None if evidence == "missing" else "reset@example.com"
    user = target(state, email=email)
    if email:
        EmailAddress.objects.create(user=user, email=email, verified=evidence == "verified")
    result = cms.post(
        f"{USERS}/{user.pk}/reset-password",
        {"password1": PASSWORD, "password2": PASSWORD},
        format="json",
    )
    user.refresh_from_db()
    if state in {"deletion_requested", "anonymized"}:
        assert result.status_code == 409
        assert result.json()["errors"][0]["code"] == "account_state_conflict"
    elif evidence == "verified":
        assert result.status_code == 409
        assert result.json()["errors"][0]["code"] == "email_recovery_required"
    else:
        assert result.status_code == 200
        assert result.json() == {"data": {"id": str(user.pk), "status": state}}
        assert user.check_password(PASSWORD)
        assert AuditLog.objects.filter(user=user, action="user.password_reset").count() == 1
    assert user.status == state and user.email == email
    assert user.email_verified == (evidence == "verified")
    assert not mailoutbox


def test_list_filters_status_before_pagination_and_combines_search(cms):
    suspended = User.objects.create_user(
        "locked@example.com",
        name="Garden Locked",
        status="suspended",
        is_active=False,
        date_joined=timezone.now() - timezone.timedelta(days=1),
    )
    User.objects.bulk_create(
        [User(email=f"pending-{index}@example.com", name="Garden Pending") for index in range(30)]
    )
    User.objects.create_user("other@example.com", name="Other", status="suspended", is_active=False)
    unfiltered = cms.get(USERS, {"search": "Garden"}).json()
    assert len(unfiltered["data"]) == 30
    assert str(suspended.pk) not in [row["id"] for row in unfiltered["data"]]

    response = cms.get(USERS, {"status": "suspended", "search": "Garden"})
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["data"]] == [str(suspended.pk)]
    assert response.json()["meta"]["total"] == 1
    assert cms.get(USERS, {"status": "invalid"}).status_code == 400


def test_projection_search_and_batch_verification(cms, django_assert_max_num_queries):
    from members.models import MemberProfile

    user = target("active", email="projected@example.com")
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    MemberProfile.objects.create(user=user, phone_number="0987654321")
    for query in ("091 234 5678", "+84912345678", "123456", "0987654321", "projected"):
        result = cms.get(USERS, {"search": query}).json()
        row = next(item for item in result["data"] if item["id"] == str(user.pk))
        assert row["phone"] == "0987654321" and row["login_phone_number"] == "+84912345678"
        assert row["email_verified"] is True
        assert "points_balance" in row and "has_csa_customer" in row
    details = cms.get(f"{USERS}/{user.pk}").json()["data"]
    assert details["login_phone_number"] == "+84912345678" and details["email_verified"]
    assert details["phone"] == "0987654321" and "referral_code" in details
    for i in range(10):
        member = User.objects.create_user(f"batch{i}@example.com", name="Batch")
        EmailAddress.objects.create(user=member, email=member.email, verified=True)
    with django_assert_max_num_queries(6):
        response = cms.get(USERS)
    assert response.status_code == 200
    assert response.json()["meta"] == {"page": 1, "page_size": 30, "total": 12}


@pytest.mark.parametrize("action", ["activate", "suspend", "reactivate", "reset-password"])
def test_actions_validate_body_and_missing_target(cms, action):
    from uuid import uuid4

    user = target()
    path = f"{USERS}/{user.pk}/{action}"
    assert cms.post(path, {"is_active": True}, format="json").status_code == 400
    assert cms.post(path, [], format="json").status_code == 400
    body = {"password1": PASSWORD, "password2": PASSWORD} if action == "reset-password" else {}
    assert cms.post(f"{USERS}/{uuid4()}/{action}", body, format="json").status_code == 404
    if action == "reset-password":
        assert (
            cms.post(path, {"password1": "short", "password2": "short"}, format="json").status_code
            == 400
        )
        assert (
            cms.post(
                path, {"password1": PASSWORD, "password2": "different"}, format="json"
            ).status_code
            == 400
        )


def test_create_omitted_email_and_selected_locale(cms):
    data = cms.post(USERS, payload(locale="en"), format="json").json()["data"]
    assert data["locale"] == "en" and data["email"] is None


@pytest.mark.parametrize("stage", ["email", "audit"])
def test_create_rolls_back_partial_records(cms, monkeypatch, settings, access_token, stage):
    from django.views.debug import ExceptionReporter

    from referrals.models import ReferralAccount

    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic write error")

    before = ReferralAccount.objects.count()
    settings.DEBUG = False
    cms.raise_request_exception = False
    monkeypatch.setattr(
        EmailAddress.objects if stage == "email" else AuditLog.objects, "create", fail
    )
    result = cms.post(USERS, payload(email="transaction@example.com"), format="json")
    assert result.status_code == 500 and result.exc_info[0] is RuntimeError
    report = ExceptionReporter(result.wsgi_request, *result.exc_info).get_traceback_text()
    assert PASSWORD.strip() not in report
    assert access_token.token not in report
    assert not User.objects.filter(email="transaction@example.com").exists()
    assert not EmailAddress.objects.filter(email="transaction@example.com").exists()
    assert ReferralAccount.objects.count() == before


@pytest.mark.parametrize(
    "action,start",
    [("activate", "pending_activation"), ("suspend", "active"), ("reactivate", "suspended")],
)
@pytest.mark.parametrize("stage", ["event", "audit"])
def test_state_rolls_back_if_event_or_audit_fails(cms, monkeypatch, action, start, stage):
    def fail(*args, **kwargs):
        raise RuntimeError("Synthetic write error")

    user = target(start)
    cms.raise_request_exception = False
    monkeypatch.setattr(
        IdentityEvent.objects if stage == "event" else AuditLog.objects, "create", fail
    )
    result = cms.post(f"{USERS}/{user.pk}/{action}")
    assert result.status_code == 500 and result.exc_info[0] is RuntimeError
    user.refresh_from_db()
    assert user.status == start
    assert not IdentityEvent.objects.filter(user=user).exists()
    assert not AuditLog.objects.filter(user=user).exists()


def credentials(user, application):
    from oauth2_provider.models import AccessToken, Grant, RefreshToken

    expires = timezone.now() + timezone.timedelta(minutes=5)
    access = AccessToken.objects.create(
        user=user, application=application, token=f"access-{user.pk}", expires=expires
    )
    refresh = RefreshToken.objects.create(
        user=user, application=application, token=f"refresh-{user.pk}", access_token=access
    )
    grant = Grant.objects.create(
        user=user, application=application, code=f"code-{user.pk}", expires=expires
    )
    return access, refresh, grant


def test_verified_email_recovery_preserves_existing_credentials(cms, client, oauth_application):
    user = target("active", email="reset-verified@example.com")
    EmailAddress.objects.create(user=user, email=user.email, verified=True)
    original_hash = user.password
    access, refresh, grant = credentials(user, oauth_application)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")

    response = cms.post(
        f"{USERS}/{user.pk}/reset-password",
        {"password1": PASSWORD, "password2": PASSWORD},
        format="json",
    )

    assert response.status_code == 409
    assert response.json()["errors"][0]["code"] == "email_recovery_required"
    user.refresh_from_db()
    assert user.password == original_hash
    assert client.get("/account").status_code == 200
    access.refresh_from_db()
    refresh.refresh_from_db()
    grant.refresh_from_db()
    assert refresh.revoked is None
    assert not AuditLog.objects.filter(user=user, action="user.password_reset").exists()


@pytest.mark.parametrize("failure", [None, "revoke", "audit"])
def test_reset_credential_boundary_and_json_error_reporting(
    cms,
    client,
    active_user,
    oauth_application,
    monkeypatch,
    settings,
    access_token,
    failure,
):
    from django.views.debug import ExceptionReporter

    from identity.passwords import revoke_core_credentials

    user = target("active")
    original_hash = user.password
    access, refresh, grant = credentials(user, oauth_application)
    other_access, other_refresh, other_grant = credentials(active_user, oauth_application)
    client.force_login(user, backend="identity.backends.CoreAuthenticationBackend")
    assert client.get("/account").status_code == 200

    def fail(*args, **kwargs):
        if failure == "revoke":
            revoke_core_credentials(**kwargs)
        raise RuntimeError("Synthetic write error")

    if failure == "revoke":
        monkeypatch.setattr("identity.admin_services.revoke_core_credentials", fail)
    elif failure == "audit":
        monkeypatch.setattr(AuditLog.objects, "create", fail)
    settings.DEBUG = False
    cms.raise_request_exception = False
    response = cms.post(
        f"{USERS}/{user.pk}/reset-password",
        {"password1": PASSWORD, "password2": PASSWORD},
        format="json",
    )
    user.refresh_from_db()
    if failure:
        assert response.status_code == 500 and response.exc_info[0] is RuntimeError
        report = ExceptionReporter(response.wsgi_request, *response.exc_info).get_traceback_text()
        assert "Secure-Harvest-42!" not in report
        assert access_token.token not in report
        assert user.password == original_hash
        access.refresh_from_db()
        refresh.refresh_from_db()
        grant.refresh_from_db()
        assert refresh.revoked is None
        assert not AuditLog.objects.filter(user=user, action="user.password_reset").exists()
    else:
        assert response.status_code == 200
        assert (
            PASSWORD.encode() not in response.content
            and user.password.encode() not in response.content
        )
        assert not user.check_password("Previous-password-42!") and user.check_password(PASSWORD)
        assert client.get("/account").status_code == 302
        assert not type(access).objects.filter(pk=access.pk).exists()
        assert not type(grant).objects.filter(pk=grant.pk).exists()
        assert not type(refresh).objects.filter(pk=refresh.pk, revoked__isnull=True).exists()
        assert AuditLog.objects.get(user=user, action="user.password_reset").context == {
            "actor_user_id": str(active_user.pk)
        }
    other_access.refresh_from_db()
    other_refresh.refresh_from_db()
    other_grant.refresh_from_db()
    assert other_refresh.revoked is None


def test_domain_checks_persisted_actor_and_target(cms, active_user):
    from django.core.exceptions import PermissionDenied

    from identity.admin_services import AccountConflict, change_account_state
    from identity.services import transition_account

    user = target("active")
    transition_account(user, "suspended")
    with pytest.raises(AccountConflict, match="account_state_conflict"):
        change_account_state(actor=active_user, user_id=user.pk, action="activate")
    active_user.groups.clear()
    with pytest.raises(PermissionDenied):
        change_account_state(actor=active_user, user_id=user.pk, action="suspend")


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("shared", ["phone", "email"])
def test_concurrent_creates_keep_at_most_one_identity(cms, access_token, shared):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    token = access_token.token
    barrier = Barrier(2)

    def create(index):
        close_old_connections()
        try:
            client = APIClient()
            client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
            email = "same@example.com" if shared == "email" else f"same{index}@example.com"
            phone = f"090123456{index}" if shared == "email" else "0901234567"
            barrier.wait(timeout=5)
            return client.post(
                USERS, payload(email=email, login_phone_number=phone), format="json"
            ).status_code
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(create, range(2))) == [201, 400]
    assert User.objects.exclude(pk=access_token.user_id).count() == 1
    assert AuditLog.objects.filter(action="user.created").count() == 1


@pytest.mark.parametrize(
    "privilege",
    ["self", "superuser", "staff", "website_admin", "direct_permission", "group_permission"],
)
def test_cms_reset_protects_privileged_targets_and_leaves_credentials_intact(
    cms, active_user, oauth_application, privilege
):
    from django.contrib.auth.models import Permission

    user = active_user if privilege == "self" else target("suspended")
    if privilege in {"superuser", "staff"}:
        field = f"is_{privilege}"
        setattr(user, field, True)
        user.save(update_fields=[field])
    elif privilege == "website_admin":
        user.groups.add(Group.objects.get(name="website_admin"))
    elif privilege in {"direct_permission", "group_permission"}:
        permission = Permission.objects.get(
            content_type__app_label="identity", codename="change_user"
        )
        if privilege == "direct_permission":
            user.user_permissions.add(permission)
        else:
            group = Group.objects.create(name="identity operators")
            group.permissions.add(permission)
            user.groups.add(group)

    access, refresh, grant = credentials(user, oauth_application)
    original_hash = user.password
    response = cms.post(
        f"{USERS}/{user.pk}/reset-password",
        {"password1": PASSWORD, "password2": PASSWORD},
        format="json",
    )

    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "account_target_protected"}]}
    user.refresh_from_db()
    access.refresh_from_db()
    refresh.refresh_from_db()
    grant.refresh_from_db()
    assert user.password == original_hash
    assert refresh.revoked is None
    assert not AuditLog.objects.filter(user=user, action="user.password_reset").exists()
    detail = cms.get(f"{USERS}/{user.pk}").json()["data"]
    assert "reset-password" not in detail["account_actions"]


@pytest.mark.parametrize("role", ["website_admin", "staff", "superuser"])
@pytest.mark.parametrize(
    ("action", "state"),
    [
        ("activate", "pending_activation"),
        ("suspend", "active"),
        ("reactivate", "suspended"),
        ("reset-password", "active"),
    ],
)
def test_admin_targets_are_read_only(cms, role, action, state):
    user = target(state, **({f"is_{role}": True} if role != "website_admin" else {}))
    if role == "website_admin":
        user.groups.add(Group.objects.get(name="website_admin"))
    original_hash = user.password
    endpoint = f"{USERS}/{user.pk}"
    body = {"password1": PASSWORD, "password2": PASSWORD} if action == "reset-password" else {}
    response = cms.post(f"{endpoint}/{action}", body, format="json")
    assert response.status_code == 409
    assert response.json() == {"errors": [{"code": "account_target_protected"}]}
    user.refresh_from_db()
    assert (user.status, user.password) == (state, original_hash)
    assert not AuditLog.objects.filter(user=user).exists()
    assert not IdentityEvent.objects.filter(user=user).exists()
    detail = cms.get(endpoint)
    assert detail.status_code == 200
    assert detail.json()["data"]["account_actions"] == []
    assert detail.json()["data"]["status"] == state


@pytest.mark.parametrize(
    ("state", "verified", "actions"),
    [
        ("pending_activation", False, ["activate", "reset-password"]),
        ("active", False, ["suspend", "reset-password"]),
        ("suspended", False, ["reactivate", "reset-password"]),
        ("active", True, ["suspend"]),
        ("deletion_requested", False, []),
        ("anonymized", False, []),
    ],
)
def test_detail_reports_account_actions_for_regular_users(cms, state, verified, actions):
    user = target(state, email="capabilities@example.com" if verified else None)
    if verified:
        EmailAddress.objects.create(user=user, email=user.email, verified=True)

    detail = cms.get(f"{USERS}/{user.pk}")
    assert detail.status_code == 200
    assert detail.json()["data"]["account_actions"] == actions


def test_verification_projection_matches_only_the_current_email(cms):
    user = target("active", email="current@example.com")
    EmailAddress.objects.create(user=user, email="previous@example.com", verified=True)
    result = cms.get(USERS, {"search": user.email})
    assert result.status_code == 200
    row = result.json()["data"][0]
    assert row["id"] == str(user.pk)
    assert row["email_verified"] is user.email_verified is False
