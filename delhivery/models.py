from django.db import models
from core.models import TimeStampedModel

class DelhiveryShipment(TimeStampedModel):
    """
    Stores Delhivery shipment details and waybill numbers for orders.
    """
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="delhivery_shipment",
        verbose_name="Order",
    )
    waybill_number = models.CharField(
        max_length=100,
        blank=True,
        null=True,
        verbose_name="Waybill Number",
        help_text="Tracking number provided by Delhivery.",
    )
    tracking_status = models.CharField(
        max_length=100,
        default="Initiated",
        verbose_name="Tracking Status",
        db_index=True,
    )
    label_url = models.URLField(
        max_length=500,
        blank=True,
        null=True,
        verbose_name="Label URL",
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        verbose_name="Error Message / Logs",
    )

    class Meta:
        verbose_name = "Delhivery Shipment"
        verbose_name_plural = "Delhivery Shipments"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Shipment for Order {self.order.order_number} ({self.tracking_status})"
