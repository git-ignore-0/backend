from django.urls import path
from rest_framework.routers import SimpleRouter

from referrals import views

router = SimpleRouter(trailing_slash=False, use_regex_path=False)
router.register("referrals/redemptions", views.SelfRedemptionViewSet, basename="redemption")
router.register("admin/rewards", views.AdminRewardViewSet, basename="admin-reward")
router.register("admin/redemptions", views.AdminRedemptionViewSet, basename="admin-redemption")

urlpatterns = [
    path("public/rewards", views.PublicRewardListView.as_view()),
    path("referrals/account", views.SelfAccountSummaryView.as_view()),
    path("referrals/submit-code", views.SelfReferralCodeView.as_view()),
    path("referrals/invited-users", views.SelfInvitedUsersView.as_view()),
    path("referrals/points", views.SelfPointHistoryView.as_view()),
    path("admin/users", views.AdminUserListView.as_view()),
    path("admin/users/<uuid:user_id>", views.AdminUserDetailView.as_view()),
    path("admin/users/<uuid:user_id>/points", views.AdminPointHistoryView.as_view()),
] + router.urls
