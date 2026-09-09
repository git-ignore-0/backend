from django.db import transaction
from django.db.models import Case, F, IntegerField, Q, Sum, Value, When
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from content.permissions import IsWebsiteAdmin
from content.serializers import media_payload
from content.services import audit, store_reward_image
from identity.models import User
from referrals.models import (
    PointDirection,
    PointTransaction,
    RedemptionRequest,
    Referral,
    Reward,
    RewardTranslation,
)
from referrals.serializers import (
    AdminRedemptionQuerySerializer,
    AdminUserQuerySerializer,
    LocalePageQuerySerializer,
    PageQuerySerializer,
    PointTransactionWriteSerializer,
    RedemptionActionSerializer,
    RedemptionCreateSerializer,
    RedemptionRejectSerializer,
    ReferralCodeSerializer,
    RewardReorderSerializer,
    RewardWriteSerializer,
)
from referrals.services import (
    InvalidPointTransaction,
    RedemptionError,
    RedemptionTransitionConflict,
    ReferralAlreadyAssigned,
    ReferralCodeInvalid,
    RewardNotFound,
    RewardReorderConflict,
    SelfReferral,
    assign_referrer,
    complete_redemption,
    create_point_transaction,
    create_redemption,
    ensure_referral_account,
    mark_redemption_contacted,
    point_balance,
    reject_redemption,
    reorder_rewards,
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


def paginated_response(queryset, page, serialize, *, page_size=SELF_SERVICE_PAGE_SIZE):
    items, meta = paginate(queryset, page, page_size=page_size)
    return no_store(Response({"data": [serialize(item) for item in items], "meta": meta}))


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
        "template": item.template,
        "message": item.message,
        "created_at": item.created_at.isoformat(),
    }


def reward_queryset():
    return Reward.objects.select_related("image").prefetch_related("translations")


def localized_reward_payload(reward: Reward, request, locale: str) -> dict:
    translations = {item.locale: item for item in reward.translations.all()}
    selected = translations.get(locale) or translations.get("vi") or translations.get("en")
    if selected is None:
        return {}
    return {
        "id": str(reward.pk),
        "point_cost": reward.point_cost,
        "image": media_payload(reward.image, request),
        "position": reward.position,
        "requested_locale": locale,
        "content_locale": selected.locale,
        "is_fallback": selected.locale != locale,
        "name": selected.name,
        "short_description": selected.short_description,
    }


def admin_reward_payload(reward: Reward, request) -> dict:
    return {
        "id": str(reward.pk),
        "point_cost": reward.point_cost,
        "image": media_payload(reward.image, request),
        "position": reward.position,
        "is_active": reward.is_active,
        "translations": [
            {
                "locale": item.locale,
                "name": item.name,
                "short_description": item.short_description,
            }
            for item in reward.translations.all()
        ],
        "created_at": reward.created_at.isoformat(),
        "updated_at": reward.updated_at.isoformat(),
    }


def redemption_timestamps(item: RedemptionRequest) -> dict:
    result = {}
    for field in ("created_at", "contacted_at", "completed_at", "rejected_at", "updated_at"):
        value = getattr(item, field)
        result[field] = value.isoformat() if value else None
    return result


def redemption_base_payload(item: RedemptionRequest) -> dict:
    return {
        "id": str(item.pk),
        "reward_id": str(item.reward_id),
        "reward_name_vi_snapshot": item.reward_name_vi_snapshot,
        "reward_name_en_snapshot": item.reward_name_en_snapshot,
        "point_cost_snapshot": item.point_cost_snapshot,
        "status": item.status,
        "rejection_message": item.rejection_message,
        **redemption_timestamps(item),
    }


def redemption_payload(item: RedemptionRequest, *, locale: str) -> dict:
    return {
        **redemption_base_payload(item),
        "reward_name": (
            item.reward_name_en_snapshot if locale == "en" else item.reward_name_vi_snapshot
        ),
    }


def admin_redemption_payload(item: RedemptionRequest, *, detail=False) -> dict:
    data = {
        **redemption_base_payload(item),
        "user": {
            "id": str(item.user_id),
            "name": item.user.name,
            "email": item.user.email,
            "phone": user_phone(item.user),
        },
    }
    if detail:
        data.update(
            {
                "points_balance": point_balance(item.user),
                "debit_transaction": transaction_payload(item.debit_transaction)
                if item.debit_transaction_id
                else None,
                "refund_transaction": transaction_payload(item.refund_transaction)
                if item.refund_transaction_id
                else None,
                "contacted_by": safe_user(item.contacted_by) if item.contacted_by else None,
                "processed_by": safe_user(item.processed_by) if item.processed_by else None,
                "internal_note": item.internal_note,
            }
        )
    return data


def redemption_queryset():
    return RedemptionRequest.objects.select_related(
        "user",
        "user__member_profile",
        "reward",
        "debit_transaction",
        "refund_transaction",
        "contacted_by",
        "processed_by",
    )


def redemption_error_response(error: RedemptionError) -> Response:
    error_status = (
        status.HTTP_404_NOT_FOUND if isinstance(error, RewardNotFound) else status.HTTP_409_CONFLICT
    )
    return no_store(
        Response(
            {"errors": [{"code": error.code}]},
            status=error_status,
        )
    )


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


class PublicRewardListView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        query = LocalePageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        locale = query.validated_data["locale"]
        queryset = reward_queryset().filter(is_active=True, translations__isnull=False).distinct()
        return paginated_response(
            queryset,
            query.validated_data["page"],
            lambda item: localized_reward_payload(item, request, locale),
        )


class SelfRedemptionViewSet(viewsets.ViewSet):
    permission_classes = [HasSelfServiceScopes]
    lookup_url_kwarg = "redemption_id"
    lookup_value_converter = "uuid"

    def list(self, request):
        query_data = request.query_params.copy()
        if "locale" not in query_data:
            query_data["locale"] = request.user.locale
        query = LocalePageQuerySerializer(data=query_data)
        query.is_valid(raise_exception=True)
        queryset = redemption_queryset().filter(user=request.user)
        return paginated_response(
            queryset,
            query.validated_data["page"],
            lambda item: redemption_payload(item, locale=query.validated_data["locale"]),
        )

    def create(self, request):
        serializer = RedemptionCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            item, balance, created = create_redemption(
                user=request.user,
                reward_id=serializer.validated_data["reward_id"],
                idempotency_key=serializer.validated_data["idempotency_key"],
            )
        except RedemptionError as error:
            return redemption_error_response(error)
        return no_store(
            Response(
                {
                    "data": {
                        "redemption": redemption_payload(item, locale=request.user.locale),
                        "balance": balance,
                        "idempotent_replay": not created,
                    }
                },
                status=status.HTTP_201_CREATED if created else status.HTTP_200_OK,
            )
        )

    def retrieve(self, request, redemption_id=None):
        query = LocalePageQuerySerializer(
            data={"locale": request.query_params.get("locale", request.user.locale), "page": 1}
        )
        query.is_valid(raise_exception=True)
        item = get_object_or_404(
            redemption_queryset(),
            pk=redemption_id,
            user=request.user,
        )
        return no_store(
            Response({"data": redemption_payload(item, locale=query.validated_data["locale"])})
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


def admin_user_csa_fields(user):
    profile = getattr(user, "member_profile", None)
    return {
        "province_code": profile.province_code if profile else "",
        "ward_code": profile.ward_code if profile else "",
        "address": profile.address if profile else "",
        "has_csa_customer": hasattr(user, "csa_customer"),
    }


class AdminUserListView(APIView):
    permission_classes = [IsWebsiteAdmin]

    def get(self, request):
        query = AdminUserQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        search = query.validated_data["search"].strip()
        queryset = User.objects.select_related("member_profile", "csa_customer").annotate(
            points_balance=balance_expression()
        )
        if search:
            queryset = queryset.filter(
                Q(name__icontains=search)
                | Q(email__icontains=search)
                | Q(member_profile__phone_number__icontains=search)
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
                            **admin_user_csa_fields(user),
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
            **admin_user_csa_fields(user),
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
        user = get_object_or_404(
            User.objects.select_related("member_profile", "csa_customer"), pk=user_id
        )
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


def persist_reward(serializer: RewardWriteSerializer, *, reward: Reward | None = None) -> Reward:
    data = dict(serializer.validated_data)
    translations = data.pop("translations", None)
    image_supplied = "image_id" in data
    image = data.pop("image_id", None)
    if reward is None:
        last_reward = (
            Reward.objects.select_for_update().order_by("-position", "-created_at").first()
        )
        position = last_reward.position + 1 if last_reward else 0
        reward = Reward.objects.create(image=image, position=position, **data)
    else:
        if image_supplied:
            reward.image = image
        for field, value in data.items():
            setattr(reward, field, value)
        update_fields = [*data.keys(), "updated_at"]
        if image_supplied:
            update_fields.append("image")
        reward.save(update_fields=update_fields)
    if translations is not None:
        reward.translations.all().delete()
        RewardTranslation.objects.bulk_create(
            RewardTranslation(reward=reward, **item) for item in translations
        )
    return reward_queryset().get(pk=reward.pk)


class AdminRewardViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    lookup_url_kwarg = "reward_id"
    lookup_value_converter = "uuid"
    http_method_names = ["get", "post", "patch", "head", "options"]

    def list(self, request):
        query = PageQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        return paginated_response(
            reward_queryset(),
            query.validated_data["page"],
            lambda item: admin_reward_payload(item, request),
            page_size=ADMIN_PAGE_SIZE,
        )

    @transaction.atomic
    def create(self, request):
        serializer = RewardWriteSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        reward = persist_reward(serializer)
        return no_store(
            Response(
                {"data": admin_reward_payload(reward, request)},
                status=status.HTTP_201_CREATED,
            )
        )

    def retrieve(self, request, reward_id=None):
        reward = get_object_or_404(reward_queryset(), pk=reward_id)
        return no_store(Response({"data": admin_reward_payload(reward, request)}))

    @transaction.atomic
    def partial_update(self, request, reward_id=None):
        reward = get_object_or_404(Reward.objects.select_for_update(), pk=reward_id)
        serializer = RewardWriteSerializer(data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        reward = persist_reward(serializer, reward=reward)
        return no_store(Response({"data": admin_reward_payload(reward, request)}))

    @action(detail=False, methods=["post"])
    def reorder(self, request):
        serializer = RewardReorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            reorder_rewards(
                page=serializer.validated_data["page"],
                reward_ids=serializer.validated_data["reward_ids"],
                page_size=ADMIN_PAGE_SIZE,
            )
        except RewardReorderConflict as error:
            return no_store(
                Response(
                    {"errors": [{"code": "reward_reorder_conflict", "detail": str(error)}]},
                    status=status.HTTP_409_CONFLICT,
                )
            )
        return paginated_response(
            reward_queryset(),
            serializer.validated_data["page"],
            lambda item: admin_reward_payload(item, request),
            page_size=ADMIN_PAGE_SIZE,
        )

    @action(
        detail=False,
        methods=["post"],
        url_path="media",
        parser_classes=[MultiPartParser, FormParser],
    )
    def media(self, request):
        upload = request.FILES.get("image")
        if upload is None:
            raise ValidationError({"image": "An image is required."})
        try:
            asset = store_reward_image(upload, actor=request.user)
        except ValueError as error:
            raise ValidationError({"image": str(error)}) from error
        audit(actor=request.user, action="reward.media.upload", entity=asset)
        return no_store(
            Response(
                {"data": media_payload(asset, request)},
                status=status.HTTP_201_CREATED,
            )
        )


class AdminRedemptionViewSet(viewsets.ViewSet):
    permission_classes = [IsWebsiteAdmin]
    lookup_url_kwarg = "redemption_id"
    lookup_value_converter = "uuid"
    http_method_names = ["get", "post", "head", "options"]

    def list(self, request):
        query = AdminRedemptionQuerySerializer(data=request.query_params)
        query.is_valid(raise_exception=True)
        data = query.validated_data
        queryset = redemption_queryset()
        search = data["search"].strip()
        if search:
            queryset = queryset.filter(
                Q(user__email__icontains=search)
                | Q(user__member_profile__phone_number__icontains=search)
            )
        if data["status"]:
            queryset = queryset.filter(status=data["status"])
        return paginated_response(
            queryset,
            data["page"],
            admin_redemption_payload,
            page_size=ADMIN_PAGE_SIZE,
        )

    def retrieve(self, request, redemption_id=None):
        item = get_object_or_404(redemption_queryset(), pk=redemption_id)
        return no_store(Response({"data": admin_redemption_payload(item, detail=True)}))

    def _transition(self, request, redemption_id, operation):
        get_object_or_404(RedemptionRequest, pk=redemption_id)
        serializer_class = (
            RedemptionRejectSerializer
            if operation is reject_redemption
            else RedemptionActionSerializer
        )
        serializer = serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        kwargs = {
            "redemption_id": redemption_id,
            "actor": request.user,
            "internal_note": data.get("internal_note"),
        }
        if operation is reject_redemption:
            kwargs["rejection_message"] = data["rejection_message"]
        try:
            operation(**kwargs)
        except RedemptionTransitionConflict as error:
            return redemption_error_response(error)
        item = get_object_or_404(redemption_queryset(), pk=redemption_id)
        return no_store(
            Response(
                {
                    "data": {
                        "redemption": admin_redemption_payload(item, detail=True),
                    }
                }
            )
        )

    @action(detail=True, methods=["post"], url_path="mark-contacted")
    def mark_contacted(self, request, redemption_id=None):
        return self._transition(request, redemption_id, mark_redemption_contacted)

    @action(detail=True, methods=["post"])
    def complete(self, request, redemption_id=None):
        return self._transition(request, redemption_id, complete_redemption)

    @action(detail=True, methods=["post"])
    def reject(self, request, redemption_id=None):
        return self._transition(request, redemption_id, reject_redemption)
