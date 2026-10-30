from django.db import models

from core.models import TimeStampedModel


class SendStatus:
    """Lifecycle of a ShipNextShipment: our push of an order to Ship Next."""

    PENDING = "Pending"  # row exists, nothing sent yet
    SENDING = "Sending"  # a worker holds the send claim (guards double submission)
    SENT = "Sent"        # Ship Next accepted the order
    FAILED = "Failed"    # last attempt failed; error_message says why


class ShipNextShipment(TimeStampedModel):
    """An order pushed to Ship Next, which books the courier on its own platform."""

    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="shipnext_shipment",
        verbose_name="Order",
    )
    send_status = models.CharField(
        max_length=20,
        default=SendStatus.PENDING,
        db_index=True,
        verbose_name="Send status",
    )
    external_order_id = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Ship Next order ID",
        help_text="The order_id Ship Next returned when it accepted the order.",
    )
    tracking_number = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Tracking number",
        help_text="Entered by hand from the Ship Next panel once the parcel is booked.",
    )
    attempts = models.PositiveIntegerField(default=0, verbose_name="Send attempts")
    sent_at = models.DateTimeField(null=True, blank=True, verbose_name="Sent at")
    raw_response = models.JSONField(default=dict, blank=True, verbose_name="Last response")
    error_message = models.TextField(blank=True, verbose_name="Error message")

    class Meta:
        verbose_name = "Ship Next Shipment"
        verbose_name_plural = "Ship Next Shipments"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Ship Next shipment for order {self.order.order_number} ({self.send_status})"

    @property
    def tracking_status(self) -> str:
        """Status label shared with the other shipment models, which order templates read."""
        return self.send_status

    @property
    def is_sent(self) -> bool:
        return self.send_status == SendStatus.SENT
