from django.contrib import admin
from django.utils.html import format_html
from django.urls import reverse
from indiapost.models import IndiaPostBarcodePool, IndiaPostShipment


@admin.register(IndiaPostBarcodePool)
class IndiaPostBarcodePoolAdmin(admin.ModelAdmin):
    list_display = ["prefix", "current_number", "end_number", "suffix", "is_active", "updated_at"]
    list_editable = ["is_active"]


@admin.register(IndiaPostShipment)
class IndiaPostShipmentAdmin(admin.ModelAdmin):
    list_display = [
        "order_link",
        "barcode_number",
        "article_type",
        "tracking_status",
        "calculated_tariff",
        "last_event_code",
        "label_button",
        "created_at",
    ]
    list_filter = ["article_type", "tracking_status", "created_at"]
    search_fields = ["barcode_number", "order__order_number", "batch_id"]
    readonly_fields = ["raw_booking_response", "created_at", "updated_at"]

    def order_link(self, obj):
        return f"Order #{obj.order.order_number}"
    order_link.short_description = "Order"

    def label_button(self, obj):
        if obj.label_pdf:
            url = reverse("indiapost:download-label", kwargs={"pk": obj.order_id})
            return format_html('<a class="button" href="{}" target="_blank">Download PDF</a>', url)
        return "-"
    label_button.short_description = "Label"
