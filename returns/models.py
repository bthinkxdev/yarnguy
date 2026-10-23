"""Customer return requests and their reverse pickups."""

from __future__ import annotations

from decimal import Decimal

from django.db import models

from core.models import TimeStampedModel


class ReturnStatus(models.TextChoices):
    REQUESTED = "requested", "Requested"
    APPROVED = "approved", "Approved - pickup pending"
    PICKUP_SCHEDULED = "pickup_scheduled", "Pickup scheduled"
    PICKED_UP = "picked_up", "Picked up"
    RECEIVED = "received", "Received"
    REFUNDED = "refunded", "Refunded"
    REPLACED = "replaced", "Replacement sent"
    REJECTED = "rejected", "Rejected"
    CANCELLED = "cancelled", "Cancelled"


#a request in one of these states no longer holds any of the order's returnable quantity
RELEASED_STATUSES = (ReturnStatus.REJECTED, ReturnStatus.CANCELLED)


class ReturnResolution(models.TextChoices):
    """What the customer wants in exchange for the returned items."""

    REFUND = "refund", "Refund"
    REPLACEMENT = "replacement", "Replacement"


class ReturnReason(models.TextChoices):
    DAMAGED = "damaged", "Item arrived damaged"
    WRONG_ITEM = "wrong_item", "Wrong item received"
    SIZE_FIT = "size_fit", "Size / fit issue"
    NOT_AS_DESCRIBED = "not_as_described", "Not as described"
    QUALITY = "quality", "Quality not as expected"
    CHANGED_MIND = "changed_mind", "Changed my mind"
    OTHER = "other", "Other"


class ReturnRequest(TimeStampedModel):
    """One customer return request, covering one or more items of a delivered order."""

    return_number = models.CharField(max_length=40, unique=True, db_index=True)
    order = models.ForeignKey(
        "orders.Order", on_delete=models.PROTECT, related_name="return_requests", verbose_name="Order"
    )
    customer_profile = models.ForeignKey(
        "accounts.CustomerProfile",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="return_requests",
    )
    status = models.CharField(
        max_length=20, choices=ReturnStatus.choices, default=ReturnStatus.REQUESTED, db_index=True
    )
    resolution = models.CharField(
        max_length=12, choices=ReturnResolution.choices, default=ReturnResolution.REFUND,
        help_text="Chosen by the customer: money back, or the same item sent again.",
    )
    customer_note = models.TextField(blank=True)
    admin_note = models.TextField(blank=True, help_text="Rejection reason, or an internal note.")

    #reverse pickup
    carrier = models.CharField(max_length=20, blank=True, help_text="delhivery or indiapost.")
    pickup_day = models.DateField(null=True, blank=True)
    pickup_slot = models.CharField(max_length=20, blank=True)
    reverse_tracking_number = models.CharField(
        max_length=100,
        blank=True,
        db_index=True,
        help_text="Barcode / waybill of the return shipment. Before the booking succeeds this is only a reservation.",
    )
    pickup_tariff = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    pickup_error = models.TextField(blank=True)
    pickup_response = models.JSONField(default=dict, blank=True)

    replacement_tracking = models.CharField(max_length=100, blank=True, help_text="Tracking number of the replacement shipment, if any.")
    replaced_at = models.DateTimeField(null=True, blank=True)

    approved_at = models.DateTimeField(null=True, blank=True)
    picked_up_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    refunded_at = models.DateTimeField(null=True, blank=True)
    refund_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Return request"
        verbose_name_plural = "Return requests"

    def __str__(self) -> str:
        return self.return_number

    @property
    def items_value(self) -> Decimal:
        """What the returned items were bought for (excludes delivery charges)."""
        return sum((i.order_item.unit_price * i.quantity for i in self.items.all()), Decimal("0.00"))

    @property
    def has_confirmed_pickup(self) -> bool:
        """A courier pickup is actually booked (a barcode on an unbooked request is only a reservation)."""
        return bool(self.reverse_tracking_number) and self.status in (
            ReturnStatus.PICKUP_SCHEDULED, ReturnStatus.PICKED_UP, ReturnStatus.RECEIVED,
            ReturnStatus.REFUNDED, ReturnStatus.REPLACED,
        )

    @property
    def pickup_is_app_managed(self) -> bool:
        """Only India Post pickups are booked from the dashboard; other couriers' returns are handled outside it."""
        return self.carrier == "indiapost"


class ReturnItem(TimeStampedModel):
    """A product line of the order being returned, with its own quantity and reason."""

    return_request = models.ForeignKey(ReturnRequest, on_delete=models.CASCADE, related_name="items")
    order_item = models.ForeignKey("orders.OrderItem", on_delete=models.PROTECT, related_name="return_items")
    quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=20, choices=ReturnReason.choices)
    restock = models.BooleanField(
        null=True,
        blank=True,
        help_text="Decided when the return is received: True = resellable, back into stock; False = damaged.",
    )

    class Meta:
        ordering = ["pk"]

    def __str__(self) -> str:
        return f"{self.order_item.product.name} x{self.quantity}"
