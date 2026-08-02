from django.urls import path
from rest_framework.routers import SimpleRouter

from memberships import views

router = SimpleRouter(trailing_slash=False, use_regex_path=False)
router.register(
    "admin/membership-products", views.AdminProductViewSet, basename="admin-membership-product"
)
router.register(
    "admin/membership-packages", views.AdminPackageViewSet, basename="admin-membership-package"
)
router.register(
    "admin/membership-requests", views.AdminRequestViewSet, basename="admin-membership-request"
)
router.register("admin/memberships", views.AdminMembershipViewSet, basename="admin-membership")
router.register(
    "admin/membership-usage", views.AdminUsageViewSet, basename="admin-membership-usage"
)

urlpatterns = [
    path("public/membership-packages", views.PublicPackageListView.as_view()),
    path(
        "public/membership-payment-availability",
        views.PublicPaymentAvailabilityView.as_view(),
    ),
    path("memberships/requests", views.SelfMembershipRequestView.as_view()),
    path(
        "memberships/requests/<uuid:request_id>/payment-submitted",
        views.SelfPaymentSubmittedView.as_view(),
    ),
    path("memberships/current", views.SelfMembershipView.as_view()),
    path("memberships/quota", views.SelfQuotaView.as_view()),
    path("memberships/usage", views.SelfUsageHistoryView.as_view()),
    path("admin/membership-payment-settings", views.AdminPaymentSettingsView.as_view()),
    path("admin/membership-banks", views.AdminBankOptionsView.as_view()),
] + router.urls
