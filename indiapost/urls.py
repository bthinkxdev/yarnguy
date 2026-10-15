from django.urls import path
from indiapost.views import download_shipping_label, indiapost_webhook

app_name = "indiapost"

urlpatterns = [
    path("webhook/", indiapost_webhook, name="webhook"),
    path("label/<int:pk>/", download_shipping_label, name="download-label"),
]
