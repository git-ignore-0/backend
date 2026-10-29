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
    "admin/csa-purchase-requests",
    views.AdminCSAPurchaseRequestViewSet,
    basename="admin-csa-purchase-request",
)
router.register("admin/memberships", views.AdminMembershipViewSet, basename="admin-membership")
router.register("admin/csa-customers", views.AdminCSACustomerViewSet, basename="admin-csa-customer")
router.register(
    "admin/membership-usage", views.AdminUsageViewSet, basename="admin-membership-usage"
)

urlpatterns = [
    path("admin/csa-terms", views.AdminCSATermsListView.as_view()),
    path("admin/csa-terms/<uuid:document_id>", views.AdminCSATermsDetailView.as_view()),
    path(
        "admin/csa-terms/<uuid:document_id>/clone",
        views.AdminCSATermsCloneView.as_view(),
    ),
    path(
        "admin/csa-terms/<uuid:document_id>/publish",
        views.AdminCSATermsPublishView.as_view(),
    ),
    path("admin/csa-coupons", views.AdminCouponListView.as_view()),
    path("admin/csa-coupons/options", views.AdminCouponOptionsView.as_view()),
    path("admin/csa-coupons/preview", views.AdminCouponPreviewView.as_view()),
    path("admin/csa-coupons/<uuid:coupon_id>", views.AdminCouponDetailView.as_view()),
    path(
        "admin/csa-coupons/<uuid:coupon_id>/<str:action>",
        views.AdminCouponActivationView.as_view(),
    ),
    path(
        "admin/membership-price-options/<uuid:option_id>/payment-plans",
        views.AdminPaymentPlanListView.as_view(),
    ),
    path(
        "admin/membership-price-options/<uuid:option_id>/payment-plans/<uuid:plan_id>",
        views.AdminPaymentPlanDetailView.as_view(),
    ),
    path(
        "admin/membership-price-options/<uuid:option_id>/payment-plans/<uuid:plan_id>/<str:action>",
        views.AdminPaymentPlanActivationView.as_view(),
    ),
    path(
        "admin/csa-purchase-requests/<uuid:request_id>/payments/<int:sequence>/confirm",
        views.AdminCSAPaymentConfirmView.as_view(),
    ),
    path(
        "admin/csa-purchase-requests/<uuid:request_id>/payments/<int:sequence>/reject",
        views.AdminCSAPaymentRejectView.as_view(),
    ),
    path(
        "admin/membership-payment-settings",
        views.AdminMembershipPaymentSettingsView.as_view(),
    ),
    path("admin/membership-banks", views.AdminMembershipBanksView.as_view()),
    path("public/membership-packages", views.PublicPackageListView.as_view()),
    path("public/csa-terms", views.PublicCSATermsView.as_view()),
    path(
        "public/csa-payment-quotes",
        views.PublicCSAPaymentQuoteView.as_view(),
    ),
    path(
        "public/csa-purchase-requests",
        views.PublicCSAPurchaseRequestCreateView.as_view(),
    ),
    path(
        "public/csa-purchase-requests/<uuid:request_id>/confirm-payment",
        views.PublicCSAPurchasePaymentConfirmView.as_view(),
    ),
    path(
        "public/csa-purchase-requests/confirm-transfer",
        views.PublicCSAPaymentQuoteConfirmView.as_view(),
    ),
    path(
        "public/csa-contract-tracker/lookup",
        views.PublicCSAContractTrackerLookupView.as_view(),
    ),
    path(
        "public/csa-contracts/verify",
        views.PublicCSAContractVerificationView.as_view(),
    ),
    path(
        "public/csa-contract-tracker/contract",
        views.PublicCSAContractTrackerContractView.as_view(),
    ),
    path(
        "public/csa-contract-tracker/contract/pdf",
        views.PublicCSAContractTrackerPDFView.as_view(),
    ),
    path("memberships/current", views.SelfMembershipView.as_view()),
    path(
        "memberships/<uuid:membership_id>/contract",
        views.SelfMembershipContractView.as_view(),
    ),
    path(
        "memberships/<uuid:membership_id>/contract/pdf",
        views.SelfMembershipContractPDFView.as_view(),
    ),
    path("memberships/quota", views.SelfQuotaView.as_view()),
    path("memberships/usage", views.SelfUsageHistoryView.as_view()),
] + router.urls
