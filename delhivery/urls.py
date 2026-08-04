from django.urls import path
from delhivery.views import delhivery_webhook

app_name = 'delhivery'

urlpatterns = [
    path('webhook/', delhivery_webhook, name='webhook'),
]
