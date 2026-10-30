from django.urls import path

from shipnext.views import shipnext_callback

app_name = "shipnext"

urlpatterns = [
    path("webhook/<str:order_ref>/", shipnext_callback, name="webhook"),
]
