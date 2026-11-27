from django.urls import path

from farms import views

urlpatterns = [
    path(
        "admin/farms/<uuid:farm_id>/stock-batches/bulk", views.AdminFarmBulkStockBatchView.as_view()
    ),
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
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/batches",
        views.AdminFarmStockBatchListView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>",
        views.AdminFarmProductDetailView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/remove",
        views.AdminFarmProductRemoveView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/re-add",
        views.AdminFarmProductReaddView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/history",
        views.AdminFarmProductHistoryView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/batches/<uuid:batch_id>",
        views.AdminFarmStockBatchDetailView.as_view(),
    ),
    path(
        "admin/farms/<uuid:farm_id>/products/<uuid:product_id>/batches/<uuid:batch_id>/cancel",
        views.AdminFarmStockBatchCancelView.as_view(),
    ),
]
