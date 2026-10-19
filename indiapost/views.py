import json
import logging
from django.conf import settings
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from dashboard.access import dashboard_required
from indiapost.models import IndiaPostShipment
from orders.models import Order, OrderStatus
from orders.services import ORDER_STATUS_RANK, transition_order_status

logger = logging.getLogger(__name__)

#map India Post event_code / event_description to our internal OrderStatus
INDIA_POST_TO_ORDER_STATUS = {
    "ITEM_BOOKED": OrderStatus.READY_TO_SHIP,
    "BOOKED": OrderStatus.READY_TO_SHIP,
    "ITEM_DISPATCHED": OrderStatus.IN_TRANSIT,
    "DISPATCHED": OrderStatus.IN_TRANSIT,
    "BAG_CLOSE": OrderStatus.IN_TRANSIT,
    "BAG_RECEIVED": OrderStatus.IN_TRANSIT,
    "ITEM_RECEIVED": OrderStatus.IN_TRANSIT,
    "TAKEN_OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
    "OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
    "DELIVERED": OrderStatus.DELIVERED,
    "ITEM_DELIVERED": OrderStatus.DELIVERED,
    "ITEM_RETURNED": OrderStatus.CANCELLED,
    "RETURNED": OrderStatus.CANCELLED,
    "RTO": OrderStatus.CANCELLED,
    "RTA": OrderStatus.CANCELLED,
}


@csrf_exempt
@require_POST
def indiapost_webhook(request):
    """
    Receives real-time delivery tracking webhook events from India Post.
    """
    #fail-closed authentication if webhook token is configured
    expected_token = getattr(settings, "INDIA_POST_WEBHOOK_TOKEN", "").strip()
    if expected_token:
        provided_token = (
            request.headers.get("X-IndiaPost-Token")
            or request.headers.get("Authorization", "").replace("Bearer ", "").strip()
            or request.GET.get("token", "").strip()
        )
        if provided_token != expected_token:
            logger.warning("India Post webhook authentication failed.")
            return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("India Post webhook received invalid JSON.")
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

    #in bulk or single event shape
    events = payload if isinstance(payload, list) else [payload]
    processed_count = 0

    for event in events:
        if not isinstance(event, dict):
            continue

        barcode = (
            event.get("article_number")
            or event.get("barcode_no")
            or event.get("article_no")
            or ""
        ).strip()

        if not barcode:
            continue

        shipment = IndiaPostShipment.objects.filter(barcode_number=barcode).select_related("order").first()
        if not shipment:
            logger.info(f"India Post webhook: barcode {barcode} not found in database.")
            continue

        event_code = (event.get("event_code") or event.get("event") or "").strip().upper()
        event_desc = event.get("event_description") or event.get("remarks") or ""
        event_time_str = event.get("event_date") or event.get("event_time") or event.get("date")

        #deduplication: skip if identical event was already recorded
        if shipment.last_event_code == event_code and shipment.last_event_description == event_desc[:255]:
            logger.info(f"India Post webhook: duplicate event {event_code} for barcode {barcode}, skipping.")
            continue

        shipment.last_event_code = event_code
        shipment.last_event_description = event_desc[:255]
        shipment.last_status_at = timezone.now()
        shipment.tracking_status = event_code or shipment.tracking_status
        shipment.save(update_fields=["last_event_code", "last_event_description", "last_status_at", "tracking_status", "updated_at"])

        # Map to OrderStatus
        target_status = INDIA_POST_TO_ORDER_STATUS.get(event_code)
        if target_status and shipment.order:
            order = shipment.order
            current_rank = ORDER_STATUS_RANK.get(order.order_status, 0)
            target_rank = ORDER_STATUS_RANK.get(target_status, 0)

            is_return_or_cancel = target_status == OrderStatus.CANCELLED
            #prevent regressions and protect already delivered orders from returning/cancellation
            if is_return_or_cancel and order.order_status == OrderStatus.DELIVERED:
                logger.warning(
                    f"India Post webhook: Refusing to cancel already delivered order {order.order_number} for event {event_code}"
                )
            elif (is_return_or_cancel and order.order_status != OrderStatus.CANCELLED) or target_rank >= current_rank:
                try:
                    transition_order_status(
                        order=order,
                        new_status=target_status,
                        force=True,
                        actor=None,
                        note=f"India Post Webhook scan: {event_code} ({event_desc})",
                    )
                except Exception as exc:
                    logger.warning(f"Error transitioning order {order.order_number} to {target_status}: {exc}")

        processed_count += 1

    return JsonResponse({"status": "success", "processed_events": processed_count})


@dashboard_required
def download_shipping_label(request, pk: int):
    """
    Downloads the saved India Post shipping label PDF for an order.
    Requires dashboard access.
    """
    try:
        shipment = IndiaPostShipment.objects.get(order_id=pk)
    except IndiaPostShipment.DoesNotExist:
        raise Http404("Shipment not found for this order.")

    if not shipment.label_pdf:
        #try generating label on-the-fly
        from indiapost.services import generate_and_save_shipping_label
        if not generate_and_save_shipping_label(shipment):
            raise Http404("Label PDF not generated yet.")

    return FileResponse(shipment.label_pdf.open("rb"), as_attachment=True, filename=f"label_{shipment.barcode_number}.pdf")
