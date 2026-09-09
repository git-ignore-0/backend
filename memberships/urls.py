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
router.register("admin/memberships", views.AdminMembershipViewSet, basename="admin-membership")
router.register("admin/csa-customers", views.AdminCSACustomerViewSet, basename="admin-csa-customer")
router.register(
    "admin/membership-usage", views.AdminUsageViewSet, basename="admin-membership-usage"
)

urlpatterns = [
    path("public/membership-packages", views.PublicPackageListView.as_view()),
    path("memberships/current", views.SelfMembershipView.as_view()),
    path("memberships/quota", views.SelfQuotaView.as_view()),
    path("memberships/usage", views.SelfUsageHistoryView.as_view()),
] + router.urls
