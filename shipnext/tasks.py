"""Celery tasks for the Ship Next integration."""

from __future__ import annotations

import logging

from celery import shared_task

from orders.models import Order
from shipnext.services import ShipNextError, send_order

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY_SECONDS = 1800


@shared_task(bind=True, max_retries=5)
def send_order_to_shipnext(self, order_id: int) -> bool:
    """
    Send an order to Ship Next. Transient failures are retried with exponential backoff;
    permanent ones (bad address, rejected order, missing config) are recorded on the
    shipment for the dashboard and not retried.
    """
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        logger.error("Cannot send to Ship Next: order %s does not exist.", order_id)
        return False

    try:
        send_order(order)
    except ShipNextError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            delay = min(60 * 2 ** self.request.retries, MAX_RETRY_DELAY_SECONDS)
            logger.warning("Ship Next send for %s will be retried in %ss: %s", order.order_number, delay, exc)
            raise self.retry(exc=exc, countdown=delay)
        logger.error("Ship Next send for %s gave up: %s", order.order_number, exc)
        return False
    return True
