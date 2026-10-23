import hmac
import json
import logging

from django.conf import settings
from django.http import FileResponse, Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from dashboard.access import dashboard_required
from indiapost.models import IndiaPostShipment
from indiapost.services import apply_tracking_event, generate_and_save_shipping_label

logger = logging.getLogger(__name__)


def _webhook_token(request) -> str:
    """Secret presented by the caller: ``X-IndiaPost-Token`` header or ``Authorization: Bearer``."""
    token = request.headers.get("X-IndiaPost-Token", "").strip()
    if token:
        return token
    auth = request.headers.get("Authorization", "")
    return auth[7:].strip() if auth.lower().startswith("bearer ") else ""


@csrf_exempt
@require_POST
def indiapost_webhook(request):
    """
    Receives real-time delivery tracking webhook events from India Post.

    Fails closed: with no ``INDIA_POST_WEBHOOK_TOKEN`` configured the endpoint refuses
    every request rather than accepting unauthenticated status changes. The secret is
    taken from a header only, never the URL, so it can't leak into access logs.
    """
    expected = (settings.INDIA_POST_WEBHOOK_TOKEN or "").strip()
    if not expected:
        logger.error("INDIA_POST_WEBHOOK_TOKEN is not configured; rejecting India Post webhook.")
        return JsonResponse({"error": "Webhook not configured"}, status=503)
    provided = _webhook_token(request)
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        logger.warning("India Post webhook authentication failed.")
        return JsonResponse({"error": "Unauthorized"}, status=401)

    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        logger.warning("India Post webhook received invalid JSON.")
        return JsonResponse({"status": "error", "message": "Invalid JSON"}, status=400)

    events = payload if isinstance(payload, list) else [payload]
    processed_count = 0

    for event in events:
        if not isinstance(event, dict):
            continue

        barcode = str(
            event.get("article_number") or event.get("barcode_no") or event.get("article_no") or ""
        ).strip()
        if not barcode:
            continue

        shipment = IndiaPostShipment.objects.filter(barcode_number=barcode).select_related("order").first()
        if shipment is None:
            logger.info("India Post webhook: barcode %s not found in database.", barcode)
            continue

        event_code = event.get("event_code") or event.get("event") or ""
        event_desc = event.get("event_description") or event.get("remarks") or ""
        if apply_tracking_event(shipment, event_code, event_desc, source="webhook"):
            processed_count += 1

    return JsonResponse({"status": "success", "processed_events": processed_count})


@dashboard_required
def download_shipping_label(request, pk: int):
    """Download the saved India Post shipping label PDF for an order (dashboard access only)."""
    shipment = IndiaPostShipment.objects.filter(order_id=pk).select_related("order").first()
    if shipment is None:
        raise Http404("Shipment not found for this order.")

    if not shipment.label_pdf and not generate_and_save_shipping_label(shipment):
        raise Http404("Label PDF not generated yet.")

    return FileResponse(
        shipment.label_pdf.open("rb"), as_attachment=True, filename=f"label_{shipment.barcode_number}.pdf"
    )
