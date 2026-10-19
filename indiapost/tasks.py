import logging
from celery import shared_task
from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def create_indiapost_shipment_for_order(self, order_id: int):
    """
    Async Celery task to create an India Post booking and label for an order.
    """
    from indiapost.services import trigger_indiapost_shipment
    from orders.models import Order

    try:
        order = Order.objects.get(pk=order_id)
    except ObjectDoesNotExist:
        logger.error(f"Cannot trigger India Post shipment: Order {order_id} does not exist.")
        return False

    try:
        success = trigger_indiapost_shipment(order)
        if not success:
            err_msg = getattr(order, "indiapost_shipment", None) and order.indiapost_shipment.error_message
            logger.warning(f"India Post booking did not succeed for Order {order.order_number}: {err_msg}")
            raise RuntimeError(f"India Post booking failed: {err_msg}")
        return success
    except Exception as exc:
        logger.error(f"Exception during India Post shipment creation for Order {order_id}: {exc}")
        raise self.retry(exc=exc)


@shared_task
def sync_indiapost_tracking_task():
    """
    Periodic task to poll India Post bulk tracking API for in-transit shipments
    and sync their latest status.
    """
    from django.utils import timezone
    from indiapost.client import IndiaPostClient
    from indiapost.models import IndiaPostShipment
    from indiapost.views import INDIA_POST_TO_ORDER_STATUS
    from orders.models import OrderStatus
    from orders.services import ORDER_STATUS_RANK, transition_order_status

    #target active, non-delivered shipments that have an allocated barcode
    active_shipments = list(
        IndiaPostShipment.objects.filter(
            barcode_number__isnull=False,
        ).exclude(
            tracking_status__in=["DELIVERED", "Delivered", "Cancelled", "Failed"]
        ).select_related("order")[:200]
    )

    if not active_shipments:
        logger.info("No active India Post shipments to sync tracking for.")
        return 0

    barcode_map = {s.barcode_number: s for s in active_shipments if s.barcode_number}
    barcodes = list(barcode_map.keys())

    client = IndiaPostClient()
    try:
        data = client.track_bulk(barcodes)
    except Exception as exc:
        logger.error(f"Failed to fetch bulk tracking from India Post: {exc}")
        return 0

    #the API returns a list or dict of tracking summaries
    articles = data if isinstance(data, list) else data.get("tracking_data") or data.get("articles") or []
    updated_count = 0

    for item in articles:
        if not isinstance(item, dict):
            continue
        barcode = item.get("article_number") or item.get("barcode_no") or item.get("article_no")
        shipment = barcode_map.get(barcode)
        if not shipment:
            continue

        event_code = (item.get("event_code") or item.get("status") or item.get("event") or "").strip().upper()
        event_desc = item.get("event_description") or item.get("remarks") or ""

        #deduplication check
        if shipment.last_event_code == event_code and shipment.last_event_description == event_desc[:255]:
            continue

        shipment.last_event_code = event_code
        shipment.last_event_description = event_desc[:255]
        shipment.last_status_at = timezone.now()
        shipment.tracking_status = event_code or shipment.tracking_status
        shipment.save(update_fields=["last_event_code", "last_event_description", "last_status_at", "tracking_status", "updated_at"])

        target_status = INDIA_POST_TO_ORDER_STATUS.get(event_code)
        if target_status and shipment.order:
            order = shipment.order
            current_rank = ORDER_STATUS_RANK.get(order.order_status, 0)
            target_rank = ORDER_STATUS_RANK.get(target_status, 0)

            is_return_or_cancel = target_status == OrderStatus.CANCELLED
            if is_return_or_cancel and order.order_status == OrderStatus.DELIVERED:
                continue
            elif (is_return_or_cancel and order.order_status != OrderStatus.CANCELLED) or target_rank >= current_rank:
                try:
                    transition_order_status(
                        order=order,
                        new_status=target_status,
                        force=True,
                        actor=None,
                        note=f"India Post Periodic Tracking sync: {event_code} ({event_desc})",
                    )
                except Exception as exc:
                    logger.warning(f"Error transitioning order {order.order_number}: {exc}")

        updated_count += 1

    logger.info(f"India Post tracking sync updated {updated_count} shipments.")
    return updated_count
