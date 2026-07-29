from django.urls import path

from referrals import views

urlpatterns = [
    path("referrals/account", views.SelfAccountSummaryView.as_view()),
    path("referrals/submit-code", views.SelfReferralCodeView.as_view()),
    path("referrals/invited-users", views.SelfInvitedUsersView.as_view()),
    path("referrals/points", views.SelfPointHistoryView.as_view()),
    path("admin/users", views.AdminUserListView.as_view()),
    path("admin/users/<uuid:user_id>", views.AdminUserDetailView.as_view()),
    path("admin/users/<uuid:user_id>/points", views.AdminPointHistoryView.as_view()),
]
