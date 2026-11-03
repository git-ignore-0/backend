from django.urls import include, path
from rest_framework.routers import SimpleRouter

from commerce import store_views, views

router = SimpleRouter(trailing_slash=False, use_regex_path=False)
router.register(
    "admin/store-categories", views.AdminCategoryViewSet, basename="admin-store-category"
)
router.register("admin/store-products", views.AdminProductViewSet, basename="admin-store-product")

urlpatterns = [
    path("store-voucher-preview", store_views.SelfStoreVoucherPreviewView.as_view()),
    path("public/store-checkouts", store_views.PublicStoreCheckoutView.as_view()),
    path(
        "public/store-orders/<uuid:order_id>/report-transfer",
        store_views.PublicStoreTransferReportView.as_view(),
    ),
    path(
        "public/store-orders/<uuid:order_id>/cancel",
        store_views.PublicStoreOrderCancelView.as_view(),
    ),
    path(
        "public/store-orders/<uuid:order_id>",
        store_views.PublicStoreOrderDetailView.as_view(),
    ),
    path("public/store-order-lookups", store_views.PublicStoreOrderLookupView.as_view()),
    path(
        "public/store-order-lookups/current",
        store_views.PublicStoreOrderLookupCurrentView.as_view(),
    ),
    path(
        "store-orders/<uuid:order_id>/buy-again",
        store_views.SelfStoreBuyAgainView.as_view(),
    ),
    path("store-orders", store_views.SelfStoreOrderListView.as_view()),
    path("store-orders/<uuid:order_id>", store_views.SelfStoreOrderDetailView.as_view()),
    path("admin/store-orders", store_views.AdminStoreOrderListView.as_view()),
    path("admin/store-orders/<uuid:order_id>", store_views.AdminStoreOrderDetailView.as_view()),
    path(
        "admin/store-orders/<uuid:order_id>/approve",
        store_views.AdminStoreOrderApproveView.as_view(),
    ),
    path(
        "admin/store-orders/<uuid:order_id>/cancel",
        store_views.AdminStoreOrderCancelView.as_view(),
    ),
    path(
        "admin/store-orders/<uuid:order_id>/record-payment",
        store_views.AdminStoreOrderRecordPaymentView.as_view(),
    ),
    path(
        "admin/store-orders/<uuid:order_id>/refund",
        store_views.AdminStoreOrderRefundView.as_view(),
    ),
    path(
        "admin/store-orders/<uuid:order_id>/deliver",
        store_views.AdminStoreOrderDeliverView.as_view(),
    ),
    path(
        "admin/store-orders/<uuid:order_id>/restock",
        store_views.AdminStoreOrderRestockView.as_view(),
    ),
    path("public/shipping-areas", views.PublicShippingAreaListView.as_view()),
    path("public/shipping-quotes", views.PublicShippingQuoteView.as_view()),
    path("shipping-address", views.SelfShippingAddressView.as_view()),
    path(
        "admin/shipping-settings/<str:province_code>",
        views.AdminShippingSettingsView.as_view(),
    ),
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
