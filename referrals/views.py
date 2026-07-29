from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from content.permissions import IsWebsiteAdmin
from identity.models import User
from referrals.models import PointDirection, PointTransaction, Referral
from referrals.serializers import (
    AdminUserQuerySerializer,
    PageQuerySerializer,
    PointTransactionWriteSerializer,
    ReferralCodeSerializer,
)
from referrals.services import (
    InvalidPointTransaction,
    ReferralAlreadyAssigned,
    ReferralCodeInvalid,
    SelfReferral,
    assign_referrer,
    create_point_transaction,
    ensure_referral_account,
    point_balance,
)

SELF_SERVICE_PAGE_SIZE = 30
ADMIN_PAGE_SIZE = 30


class HasSelfServiceScopes(BasePermission):
    def has_permission(self, request, view):
        token = request.auth
        return bool(
            request.user
            and request.user.is_authenticated
            and token
            and {"profile", "email"}.issubset(set(token.scope.split()))
        )


def no_store(response: Response) -> Response:
    response["Cache-Control"] = "private, no-store"
    response["Pragma"] = "no-cache"
    return response


def paginate(queryset, page: int, *, page_size: int = SELF_SERVICE_PAGE_SIZE):
    total = queryset.count()
    start = (page - 1) * page_size
    return queryset[start : start + page_size], {
        "page": page,
        "page_size": page_size,
        "total": total,
    }


def safe_user(user) -> dict:
    return {"id": str(user.pk), "name": user.name}


def user_phone(user) -> str:
    profile = getattr(user, "member_profile", None)
    return profile.phone_number if profile else ""


def transaction_payload(item: PointTransaction) -> dict:
    return {
        "id": str(item.pk),
        "direction": item.direction,
        "amount": item.amount,
        "message": item.message,
        "created_at": item.created_at.isoformat(),
    }


def account_summary(user) -> dict:
    account = ensure_referral_account(user)
    relation = Referral.objects.select_related("referrer").filter(referee=user).first()
    return {
        "referral_code": account.code,
        "referrer": safe_user(relation.referrer) if relation else None,
        "can_submit_referral_code": relation is None,
        "points_balance": point_balance(user),
        "invited_count": Referral.objects.filter(referrer=user).count(),
    }


class SelfAccountSummaryView(APIView):
    permission_classes = [HasSelfServiceScopes]

    def get(self, request):
        return no_store(Response({"data": account_summary(request.user)}))


class SelfReferralCodeView(APIView):
    permission_classes = [HasSelfServiceScopes]

    def post(self, request):
        serializer = ReferralCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            assign_referrer(referee=request.user, code=serializer.validated_data["code"])
        except ReferralCodeInvalid:
            return no_store(
                Response(
                    {"errors": [{"code": "invalid_referral_code"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        except SelfReferral:
            return no_store(
                Response(
                    {"errors": [{"code": "self_referral"}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        except ReferralAlreadyAssigned:
            return no_store(
                Response(
                    {"errors": [{"code": "referrer_already_assigned"}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        return no_store(Response({"data": account_summary(request.user)}))


class SelfInvitedUsersView(APIView):
    permission_classes = [HasSelfServiceScopes]

    def get(self, request):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = Referral.objects.filter(referrer=request.user).select_related("referee")
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(
            Response(
                {
                    "data": [
                        {
                            "id": str(item.referee_id),
                            "name": item.referee.name,
                            "status": "joined",
                            "referred_at": item.created_at.isoformat(),
                        }
                        for item in items
                    ],
                    "meta": meta,
                }
            )
        )


class SelfPointHistoryView(APIView):
    permission_classes = [HasSelfServiceScopes]

    def get(self, request):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        queryset = PointTransaction.objects.filter(user=request.user)
        items, meta = paginate(queryset, query.validated_data["page"])
        return no_store(
            Response(
                {
                    "data": [transaction_payload(item) for item in items],
                    "meta": {**meta, "balance": point_balance(request.user)},
                }
            )
        )


def balance_expression():
    return Coalesce(
        Sum(
            Case(
                When(
                    point_transactions__direction=PointDirection.CREDIT,
                    then=F("point_transactions__amount"),
                ),
                When(
                    point_transactions__direction=PointDirection.DEBIT,
                    then=-F("point_transactions__amount"),
                ),
                default=Value(0),
                output_field=IntegerField(),
            )
        ),
        0,
    )


class AdminUserListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = AdminUserQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        search = query.validated_data["search"].strip()
        queryset = User.objects.select_related("member_profile").annotate(
            points_balance=balance_expression()
        )
        if search:
            queryset = queryset.filter(
                Q(email__icontains=search) | Q(member_profile__phone_number__icontains=search)
            )
        queryset = queryset.order_by("-date_joined", "-id")
        items, meta = paginate(queryset, query.validated_data["page"], page_size=ADMIN_PAGE_SIZE)
        return no_store(
            Response(
                {
                    "data": [
                        {
                            "id": str(user.pk),
                            "name": user.name,
                            "email": user.email,
                            "phone": user_phone(user),
                            "status": user.status,
                            "date_joined": user.date_joined.isoformat(),
                            "points_balance": user.points_balance,
                        }
                        for user in items
                    ],
                    "meta": meta,
                }
            )
        )


def admin_user_payload(user, invited_page: int = 1) -> tuple[dict, dict]:
    account = ensure_referral_account(user)
    source = Referral.objects.select_related("referrer").filter(referee=user).first()
    invited, invited_meta = paginate(
        Referral.objects.filter(referrer=user).select_related("referee"),
        invited_page,
        page_size=ADMIN_PAGE_SIZE,
    )
    profile = getattr(user, "member_profile", None)
    return (
        {
            "id": str(user.pk),
            "name": user.name,
            "email": user.email,
            "phone": profile.phone_number if profile else "",
            "date_joined": user.date_joined.isoformat(),
            "status": user.status,
            "referral_code": account.code,
            "referrer": safe_user(source.referrer) if source else None,
            "source": "referred" if source else "organic",
            "invited_count": invited_meta["total"],
            "invited_users": [
                {
                    "id": str(item.referee_id),
                    "name": item.referee.name,
                    "email": item.referee.email,
                    "status": item.referee.status,
                    "referred_at": item.created_at.isoformat(),
                }
                for item in invited
            ],
            "points_balance": point_balance(user),
        },
        invited_meta,
    )


class AdminUserDetailView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, user_id):
        query = PageQuerySerializer(data={"page": request.query_params.get("invited_page", 1)})
        query.is_valid(raise_exception=True)
        user = get_object_or_404(User.objects.select_related("member_profile"), pk=user_id)
        data, invited_meta = admin_user_payload(user, query.validated_data["page"])
        return no_store(Response({"data": data, "meta": {"invited": invited_meta}}))


class AdminPointHistoryView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request, user_id):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        user = get_object_or_404(User, pk=user_id)
        queryset = PointTransaction.objects.filter(user=user).select_related("actor")
        items, meta = paginate(queryset, query.validated_data["page"], page_size=ADMIN_PAGE_SIZE)
        return no_store(
            Response(
                {
                    "data": [
                        {
                            **transaction_payload(item),
                            "actor": safe_user(item.actor) if item.actor else None,
                        }
                        for item in items
                    ],
                    "meta": {**meta, "balance": point_balance(user)},
                }
            )
        )

    def post(self, request, user_id):
        user = get_object_or_404(User, pk=user_id)
        serializer = PointTransactionWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            item, balance = create_point_transaction(
                user=user,
                actor=request.user,
                direction=data["direction"],
                amount=data["amount"],
                message=data["message"],
                related_referral=data.get("related_referral_id"),
            )
        except InvalidPointTransaction as error:
            return no_store(
                Response(
                    {"errors": [{"code": "invalid_point_transaction", "detail": str(error)}]},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            )
        return no_store(
            Response(
                {
                    "data": {
                        "transaction": {
                            **transaction_payload(item),
                            "actor": safe_user(item.actor) if item.actor else None,
                        },
                        "balance": balance,
                    }
                },
                status=status.HTTP_201_CREATED,
            )
        )
