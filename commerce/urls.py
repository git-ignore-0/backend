from django.urls import include, path
from rest_framework.routers import SimpleRouter

from commerce import views

router = SimpleRouter(trailing_slash=False, use_regex_path=False)
router.register(
    "admin/store-categories", views.AdminCategoryViewSet, basename="admin-store-category"
)
router.register("admin/store-products", views.AdminProductViewSet, basename="admin-store-product")

urlpatterns = [
    path("public/store-categories", views.PublicStoreCategoryListView.as_view()),
    path("public/store-products", views.PublicStoreProductListView.as_view()),
    path(
        "public/store-products/<slug:slug>",
        views.PublicStoreProductDetailView.as_view(),
    ),
    path("admin/store-products/media", views.AdminProductMediaUploadView.as_view()),
    path(
        "admin/store-products/media/<uuid:media_id>",
        views.AdminProductMediaDetailView.as_view(),
    ),
    path("", include(router.urls)),
]
