from django.contrib import admin
from .models import DelhiveryShipment

@admin.register(DelhiveryShipment)
class DelhiveryShipmentAdmin(admin.ModelAdmin):
    list_display = ("order", "waybill_number", "tracking_status", "created_at", "updated_at")
    list_filter = ("tracking_status", "created_at")
    search_fields = ("order__order_number", "waybill_number", "tracking_status")
    readonly_fields = ("created_at", "updated_at")
    raw_id_fields = ("order",)
