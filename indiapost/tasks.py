"""Celery tasks for the India Post integration."""

from __future__ import annotations

import logging

from celery import shared_task
from django.db.models import F

from indiapost.client import IndiaPostClient, IndiaPostClientError
from indiapost.models import IndiaPostShipment, ShipmentStatus
from indiapost.services import (
    CLOSED_ORDER_STATUSES,
    IndiaPostBookingError,
    apply_tracking_event,
    book_shipment,
    extract_tracking_items,
)
from orders.models import Order

logger = logging.getLogger(__name__)

#India Post accepts up to 500 articles per tracking call
TRACKING_BATCH_SIZE = 200
MAX_RETRY_DELAY_SECONDS = 1800


@shared_task(bind=True, max_retries=5)
def create_indiapost_shipment_for_order(self, order_id: int) -> bool:
    """
    Book an order with India Post. Transient failures are retried with exponential
    backoff; permanent ones (bad address, missing config, rejected booking) are recorded
    on the shipment for the admin and not retried.
    """
    order = Order.objects.filter(pk=order_id).first()
    if order is None:
        logger.error("Cannot book with India Post: order %s does not exist.", order_id)
        return False

    try:
        book_shipment(order)
    except IndiaPostBookingError as exc:
        if exc.retryable and self.request.retries < self.max_retries:
            delay = min(60 * 2 ** self.request.retries, MAX_RETRY_DELAY_SECONDS)
            logger.warning("India Post booking for %s will be retried in %ss: %s", order.order_number, delay, exc)
            raise self.retry(exc=exc, countdown=delay)
        logger.error("India Post booking for %s gave up: %s", order.order_number, exc)
        return False
    return True


@shared_task
def sync_indiapost_tracking_task() -> int:
    """
    Poll India Post's bulk tracking API for every shipment whose order is still open.

    Least recently synced shipments go first, so no shipment starves when there are
    more open shipments than fit one run. Returns the number of shipments updated.
    """
    open_shipments = (
        IndiaPostShipment.objects.filter(barcode_number__isnull=False)
        .exclude(tracking_status__in=ShipmentStatus.NOT_BOOKED)
        .exclude(order__order_status__in=CLOSED_ORDER_STATUSES)
        .select_related("order")
        .order_by(F("last_status_at").asc(nulls_first=True), "pk")
    )

    client = IndiaPostClient()
    updated = 0
    batch: list[IndiaPostShipment] = []

    def flush() -> None:
        nonlocal updated, batch
        if not batch:
            return
        by_barcode = {s.barcode_number: s for s in batch}
        batch = []
        try:
            data = client.track_bulk(list(by_barcode))
        except IndiaPostClientError as exc:
            logger.error("India Post bulk tracking failed: %s", exc)
            return
        for item in extract_tracking_items(data):
            shipment = by_barcode.get(item["article_number"])
            if shipment is None:
                continue
            if apply_tracking_event(
                shipment, item["event_code"], item["event_description"], source="tracking sync", returning=item["rts"]
            ):
                updated += 1

    for shipment in open_shipments.iterator(chunk_size=TRACKING_BATCH_SIZE):
        batch.append(shipment)
        if len(batch) >= TRACKING_BATCH_SIZE:
            flush()
    flush()

    logger.info("India Post tracking sync updated %s shipments.", updated)
    return updated
