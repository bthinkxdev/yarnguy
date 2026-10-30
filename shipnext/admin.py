from django.contrib import admin

from shipnext.models import ShipNextShipment


@admin.register(ShipNextShipment)
class ShipNextShipmentAdmin(admin.ModelAdmin):
    list_display = ("order", "send_status", "external_order_id", "tracking_number", "attempts", "sent_at")
    list_filter = ("send_status",)
    search_fields = ("order__order_number", "external_order_id", "tracking_number")
    readonly_fields = ("raw_response",)
