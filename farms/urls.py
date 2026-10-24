from django.urls import path

from farms import views

urlpatterns = [
    path("admin/farms", views.AdminFarmListView.as_view()),
    path("admin/farms/media", views.AdminFarmMediaUploadView.as_view()),
    path("admin/farms/media/<uuid:media_id>", views.AdminFarmMediaDetailView.as_view()),
    path("admin/farms/<uuid:farm_id>", views.AdminFarmDetailView.as_view()),
    path("admin/farms/<uuid:farm_id>/growth-tracker", views.AdminFarmGrowthTrackerView.as_view()),
    path(
        "admin/farms/<uuid:farm_id>/available-products",
        views.AdminFarmAvailableProductListView.as_view(),
    ),
    path("admin/farms/<uuid:farm_id>/products", views.AdminFarmProductListView.as_view()),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/weeks",
        views.AdminFarmWeeklySupplyListView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>",
        views.AdminFarmProductDetailView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/weeks/<str:week_start>",
        views.AdminFarmWeeklySupplyView.as_view(),
    ),
]
