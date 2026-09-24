from django.urls import path

from jobs import views

urlpatterns = [
    path("refresh/", views.refresh, name="refresh"),
    path("api/sync", views.sync_status, name="sync_status"),
]
