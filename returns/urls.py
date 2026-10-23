"""Customer return routes (mounted at /returns/)."""

from __future__ import annotations

from django.urls import path

from returns import views

app_name = "returns"

urlpatterns = [
    path("", views.return_list_view, name="list"),
    path("order/<int:order_id>/", views.return_request_view, name="request"),
    path("<int:pk>/", views.return_detail_view, name="detail"),
    path("<int:pk>/cancel/", views.return_cancel_view, name="cancel"),
]
