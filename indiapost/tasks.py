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
            logger.warning(f"India Post booking did not succeed for Order {order.order_number}.")
        return success
    except Exception as exc:
        logger.error(f"Exception during India Post shipment creation for Order {order_id}: {exc}")
        raise self.retry(exc=exc)
