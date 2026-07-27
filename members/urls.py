from django.urls import path

from members import views

urlpatterns = [
    path("wards", views.wards, name="administrative-wards"),
]
