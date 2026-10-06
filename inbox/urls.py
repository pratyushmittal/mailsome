from django.urls import path

from inbox import views

urlpatterns = [
    path("", views.inbox, name="inbox"),
    path("tabs/new/", views.tab_edit, name="tab_new"),
    path("tabs/<int:tab_id>/edit/", views.tab_edit, name="tab_edit"),
    path("tabs/order/", views.reorder_tabs, name="tab_order"),
    path("senders/edit/", views.sender_edit, name="sender_edit"),
    path("compose/", views.compose, name="compose"),
    path(
        "messages/<str:message_id>/attachments/", views.attachments, name="attachments"
    ),
    path(
        "messages/<str:message_id>/attachments/<str:part_id>/",
        views.download_attachment,
        name="download_attachment",
    ),
    path("messages/<str:message_id>/", views.message, name="message"),
    path(
        "messages/<str:message_id>/history/",
        views.message_history,
        name="message_history",
    ),
    path("messages/<str:message_id>/body/", views.message_body, name="message_body"),
    path("messages/<str:message_id>/archive/", views.archive, name="archive"),
    path("messages/<str:message_id>/read/", views.mark_read, name="mark_read"),
    path("messages/<str:message_id>/reply/", views.compose, name="reply"),
    path(
        "messages/<str:message_id>/unsubscribe/", views.unsubscribe, name="unsubscribe"
    ),
]
