from django.urls import path

from members import views

urlpatterns = [
    path("provinces", views.provinces, name="administrative-provinces"),
    path("wards", views.wards, name="administrative-wards"),
]
