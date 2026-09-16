from django.urls import path

from jobs import views

urlpatterns = [
    path("refresh/", views.refresh, name="refresh"),
    path("api/progress", views.progress, name="progress"),
]
