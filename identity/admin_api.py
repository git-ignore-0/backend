from django.core.exceptions import ValidationError as DjangoValidationError
from django.http import Http404
from django.utils.decorators import method_decorator
from django.views.decorators.debug import sensitive_post_parameters
from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import JSONParser
from rest_framework.response import Response
from rest_framework.views import APIView

from content.authentication import CoreBearerAuthentication
from content.permissions import IsWebsiteAdmin
from identity.admin_services import (
    AccountConflict,
    change_account_state,
    create_account,
    reset_account_password,
)
from identity.models import User
from identity.passwords import AccountPasswordReporterFilter
from identity.serializers import (
    AccountActionSerializer,
    AccountCreateSerializer,
    AccountPasswordSerializer,
    AccountSummarySerializer,
)


class AdminAccountAPIView(APIView):
    authentication_classes = [CoreBearerAuthentication]
    permission_classes = [IsWebsiteAdmin]
    parser_classes = [JSONParser]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if request.method == "POST":
            response["Cache-Control"] = "private, no-store"
        return response

    def handle_exception(self, exc):
        if isinstance(exc, AccountConflict):
            return Response({"errors": [{"code": str(exc)}]}, status=status.HTTP_409_CONFLICT)
        if isinstance(exc, User.DoesNotExist):
            exc = Http404()
        if isinstance(exc, DjangoValidationError):
            detail = exc.message_dict if hasattr(exc, "error_dict") else exc.messages
            exc = ValidationError(detail=detail)
        return super().handle_exception(exc)


class AdminAccountPasswordAPIView(AdminAccountAPIView):
    @method_decorator(sensitive_post_parameters("password1", "password2"))
    def dispatch(self, request, *args, **kwargs):
        if request.method == "POST":
            request.exception_reporter_filter = AccountPasswordReporterFilter()
        return super().dispatch(request, *args, **kwargs)


def create_admin_user(request):
    serializer = AccountCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    fields = serializer.validated_data
    user = create_account(
        actor=request.user,
        name=fields["name"],
        login_phone_number=fields["login_phone_number"],
        email=fields.get("email"),
        password=fields["password1"],
        locale=fields.get("locale"),
    )
    return Response({"data": AccountSummarySerializer(user).data}, status=status.HTTP_201_CREATED)


class AdminAccountStateView(AdminAccountAPIView):
    action = None

    def post(self, request, user_id):
        serializer = AccountActionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = change_account_state(actor=request.user, user_id=user_id, action=self.action)
        return Response({"data": AccountSummarySerializer(user).data})


class AdminAccountResetPasswordView(AdminAccountPasswordAPIView):
    def post(self, request, user_id):
        serializer = AccountPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = reset_account_password(
            actor=request.user, user_id=user_id, password=serializer.validated_data["password1"]
        )
        return Response({"data": {"id": str(user.pk), "status": user.status}})
