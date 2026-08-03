"""URL routing for the payments app."""

from __future__ import annotations

from django.urls import path

from payments import views

app_name = "payments"

urlpatterns = [
    path("webhooks/<str:gateway_key>/", views.payment_webhook_view, name="webhook"),
]
