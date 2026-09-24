from django.urls import path

from classifications import views

urlpatterns = [
    path("settings/importance/", views.importance_settings, name="importance_settings"),
    path("settings/context/", views.user_context, name="user_context"),
    path("settings/", views.ai_settings, name="settings"),
    path("settings/reclassify/", views.reclassify, name="reclassify"),
]
