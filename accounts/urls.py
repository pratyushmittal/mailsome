from django.urls import path

from accounts import views

urlpatterns = [
    path("auth/connect", views.connect),
    path("auth/callback", views.callback),
    path("auth/credentials", views.upload_credentials),
]
