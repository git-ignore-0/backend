from django.urls import include, path
from rest_framework.routers import SimpleRouter

from content import views

router = SimpleRouter()
router.register("admin/workshops", views.AdminWorkshopViewSet, basename="admin-workshop")
router.register("admin/testimonials", views.AdminTestimonialViewSet, basename="admin-testimonial")

urlpatterns = [
    path("public/site-settings", views.PublicSiteSettingsView.as_view()),
    path("public/tracker-farms", views.PublicTrackerFarmListView.as_view()),
    path("public/workshops", views.PublicWorkshopListView.as_view()),
    path("public/workshops/<slug:slug>", views.PublicWorkshopDetailView.as_view()),
    path("public/testimonials", views.PublicTestimonialListView.as_view()),
    path("public/testimonials/featured", views.PublicFeaturedTestimonialView.as_view()),
    path(
        "public/testimonials/<uuid:testimonial_id>",
        views.PublicTestimonialDetailView.as_view(),
    ),
    path("admin/session", views.AdminSessionView.as_view()),
    path("admin/dashboard", views.AdminDashboardView.as_view()),
    path("admin/site-settings", views.AdminSiteSettingsView.as_view()),
    path("admin/tracker-farms", views.AdminTrackerFarmListView.as_view()),
    path("admin/tracker-farms/media", views.AdminTrackerFarmMediaUploadView.as_view()),
    path(
        "admin/tracker-farms/media/<uuid:media_id>",
        views.AdminTrackerFarmMediaDetailView.as_view(),
    ),
    path("admin/tracker-farms/reorder", views.AdminTrackerFarmReorderView.as_view()),
    path(
        "admin/tracker-farms/<uuid:farm_id>",
        views.AdminTrackerFarmDetailView.as_view(),
    ),
    path("admin/media", views.AdminMediaUploadView.as_view()),
    path("", include(router.urls)),
]
