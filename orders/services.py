"""Write operations and business rules for the orders app."""

from __future__ import annotations

import random
import string
import uuid
from typing import Optional

from django.contrib.auth.models import User
from django.db import transaction

from orders.exceptions import InvalidOrderStatusTransitionError
from orders.models import Order, OrderStatus, OrderStatusHistory
from orders.signals import order_status_changed

ALLOWED_STATUS_TRANSITIONS: dict[str, set[str]] = {
    OrderStatus.PLACED: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
    OrderStatus.CONFIRMED: {OrderStatus.PENDING, OrderStatus.CANCELLED},
    OrderStatus.PENDING: {OrderStatus.READY_TO_SHIP, OrderStatus.CANCELLED},
    OrderStatus.READY_TO_SHIP: {OrderStatus.SHIPPED, OrderStatus.CANCELLED},
    OrderStatus.SHIPPED: {OrderStatus.DELIVERED, OrderStatus.CANCELLED},
    OrderStatus.DELIVERED: {OrderStatus.REFUNDED},
    OrderStatus.CANCELLED: {OrderStatus.REFUNDED},
    OrderStatus.REFUNDED: set(),
}


def generate_order_number() -> str:
    """Return a unique human-readable order number."""
    while True:
        order_number = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
        if not Order.objects.filter(order_number=order_number).exists():
            return order_number


@transaction.atomic
def transition_order_status(
    *,
    order: Order,
    new_status: str,
    actor: Optional[User] = None,
    note: str = "",
    send_notifications: bool = True,
    force: bool = False,
) -> Order:
    """
    Validate and apply an order status transition.

    Writes ``OrderStatusHistory`` atomically and emits ``order_status_changed``.
    Notifications listen to the signal — this service never calls them directly.
    """
    old_status = order.order_status
    if new_status == old_status:
        return order

    if not force:
        allowed = ALLOWED_STATUS_TRANSITIONS.get(old_status, set())
        if new_status not in allowed:
            raise InvalidOrderStatusTransitionError(
                f"Cannot transition order from {old_status} to {new_status}."
            )

    order.order_status = new_status
    order.save(update_fields=["order_status", "updated_at"])

    OrderStatusHistory.objects.create(
        order=order,
        from_status=old_status,
        to_status=new_status,
        changed_by=actor,
        note=note,
    )

    order_status_changed.send(
        sender=Order,
        order=order,
        old_status=old_status,
        new_status=new_status,
        send_notifications=send_notifications,
    )

    if new_status == OrderStatus.PENDING:
        from orders.plugins import order_pending_registry
        order_pending_registry.execute_plugins(order)

    tx = order.payment_transactions.last()
    if tx:
        from payments.models import PaymentStatus
        if new_status == OrderStatus.DELIVERED:
            tx.status = PaymentStatus.SUCCESS
            tx.save(update_fields=["status", "updated_at"])
        elif new_status == OrderStatus.CANCELLED:
            tx.status = PaymentStatus.CANCELLED
            tx.save(update_fields=["status", "updated_at"])
        elif new_status == OrderStatus.REFUNDED:
            tx.status = PaymentStatus.REFUNDED
            tx.save(update_fields=["status", "updated_at"])

    return order
