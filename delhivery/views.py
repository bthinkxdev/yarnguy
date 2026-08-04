import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django.conf import settings

from orders.models import Order, OrderStatus
from orders.services import transition_order_status

logger = logging.getLogger(__name__)

@csrf_exempt
@require_POST
def delhivery_webhook(request):
    """
    Endpoint for receiving Delhivery scan push webhooks.
    """
    #1.validate custom header authentication
    expected_token = getattr(settings, 'DELHIVERY_WEBHOOK_TOKEN', None)
    provided_token = request.headers.get('X-Delhivery-Token')
    
    if expected_token and provided_token != expected_token:
        logger.warning("Delhivery webhook authentication failed.")
        return JsonResponse({"error": "Unauthorized"}, status=401)
        
    #2.parse payload
    try:
        payload = json.loads(request.body)
    except json.JSONDecodeError:
        logger.error("Invalid JSON payload received in Delhivery webhook.")
        return JsonResponse({"error": "Invalid payload"}, status=400)
        
    logger.info(f"Received Delhivery webhook payload: {payload}")
    
    #delhivery's scan push payload usually contains Waybill and status information
    #we will assume it has 'order_id' or 'ref_num' and 'status' (e.g., 'Delivered')
    order_number = payload.get('ref_num') or payload.get('order_id')
    status_str = payload.get('status', '').lower()
    
    if not order_number:
        return JsonResponse({"error": "Missing order reference"}, status=400)
        
    #3.find Order
    order = Order.objects.filter(order_number=order_number).first()
    if not order:
        logger.warning(f"Order not found for Delhivery webhook: {order_number}")
        return JsonResponse({"error": "Order not found"}, status=404)
        
    #4.update shipment & order status logic
    from delhivery.models import DelhiveryShipment
    DelhiveryShipment.objects.filter(order=order).update(tracking_status=status_str.title() or "Updated")

    #map delhivery tracking status strings (in lowercase) to our core OrderStatus choices
    DELHIVERY_TO_ORDER_STATUS = {
        "in transit": OrderStatus.SHIPPED,
        "dispatched": OrderStatus.SHIPPED,
        "shipped": OrderStatus.SHIPPED,
        "out for delivery": OrderStatus.SHIPPED,
        "delivered": OrderStatus.DELIVERED,
        "cancelled": OrderStatus.CANCELLED,
        "rto": OrderStatus.CANCELLED,
        "rto delivered": OrderStatus.CANCELLED,
    }

    target_status = DELHIVERY_TO_ORDER_STATUS.get(status_str)

    if target_status and order.order_status != target_status:
        try:
            transition_order_status(
                order=order,
                new_status=target_status,
                note=f"Order update: {status_str.title()}",
                force=True  #force transition in case current status restricts it
            )
            logger.info(f"Order {order_number} marked as {target_status} via webhook.")
        except Exception as e:
            logger.error(f"Failed to transition order {order_number} to {target_status}: {e}")
            return JsonResponse({"error": "Failed to update order"}, status=500)

    return JsonResponse({"status": "success"})
