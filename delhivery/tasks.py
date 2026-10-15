"""Celery tasks for the delhivery app."""

from __future__ import annotations

import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name="delhivery.tasks.create_shipment_for_order")
def create_shipment_for_order(*, order_id: int) -> bool:
    """
    Async wrapper around trigger_shipment_on_confirmed.

    Runs post-commit (see orders.services.transition_order_status) so the blocking
    Delhivery API call never holds the order's DB transaction open.
    """
    from orders.models import Order
    from core.models import SiteSettings

    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        logger.error(f"create_shipment_for_order: order {order_id} not found.")
        return False

    carrier = order.get_delivery_partner()
    if not carrier:
        site_settings = SiteSettings.objects.first()
        carrier = getattr(site_settings, "shipping_carrier", "delhivery") or "delhivery"
        order.delivery_partner = carrier
        order.save(update_fields=["delivery_partner", "updated_at"])

    #if carrier is India Post, route to India Post exclusively
    if carrier == "indiapost":
        from indiapost.services import trigger_indiapost_shipment
        return trigger_indiapost_shipment(order)

    #mutual exclusivity guard: If India Post barcode is already allocated, prevent Delhivery dispatch
    if hasattr(order, "indiapost_shipment") and getattr(order.indiapost_shipment, "barcode_number", None):
        logger.warning(
            f"create_shipment_for_order: Order {order.order_number} already has active India Post "
            f"barcode ({order.indiapost_shipment.barcode_number}). Delhivery dispatch aborted."
        )
        return False

    from delhivery.services import trigger_shipment_on_confirmed
    return trigger_shipment_on_confirmed(order)
