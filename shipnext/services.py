"""Ship Next: push confirmed orders to the Ship Next platform, which books the courier itself."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import requests
from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from indiapost.services import (
    IndiaPostBookingError,
    box_dimensions,
    is_cod_order,
    line_weight_grams,
    normalize_event_name,
    resolve_recipient,
)
from orders.models import Order, OrderStatus
from orders.services import ORDER_STATUS_RANK, transition_order_status
from shipnext.models import SendStatus, ShipNextShipment

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 20
#same default India Post uses when no product on the order has a weight
DEFAULT_WEIGHT_GRAMS = 500
#a Sending claim older than this is assumed to belong to a crashed worker
CLAIM_STALE_AFTER = timedelta(minutes=10)
#sent as-is in Ship Next's financial_status: COD is "pending" (cash still to collect), prepaid is "Paid"
FINANCIAL_STATUS_COD = "pending"
FINANCIAL_STATUS_PREPAID = "Paid"
#pkg_shape values Ship Next accepts: "nrol" is a box, "rol" a cylinder
PACKAGE_SHAPE_BOX = "nrol"

#status words Ship Next may call back with -> internal order status (matched after normalize_event_name).
#"fulfilled" means the courier booking exists and the tracking number is ready.
SHIPNEXT_TO_ORDER_STATUS = {
    "FULFILLED": OrderStatus.READY_TO_SHIP,
    "BOOKED": OrderStatus.READY_TO_SHIP,
    "READY_TO_SHIP": OrderStatus.READY_TO_SHIP,
    "PICKED_UP": OrderStatus.PICKED_UP,
    "IN_TRANSIT": OrderStatus.IN_TRANSIT,
    "DISPATCHED": OrderStatus.IN_TRANSIT,
    "OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
    "DELIVERED": OrderStatus.DELIVERED,
    "CANCELLED": OrderStatus.CANCELLED,
    "RETURNED": OrderStatus.CANCELLED,
    "RTO": OrderStatus.CANCELLED,
}
#orders in these states take no further courier updates
CLOSED_ORDER_STATUSES = (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED)


class ShipNextError(Exception):
    """An order could not be sent. ``retryable`` says whether trying again later can help."""

    def __init__(self, message: str, *, retryable: bool = False, response: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.response = response  # Ship Next's response body, when it sent one


#orders in these states are not ready to be handed to a courier yet, or never will be
NOT_DISPATCHABLE_STATUSES = (
    OrderStatus.CHECKOUT_PENDING, OrderStatus.PLACED_COD, OrderStatus.CANCELLED, OrderStatus.REFUNDED,
)


def webhook_url() -> str:
    return str(settings.SHIPNEXT_WEBHOOK_URL or "").strip()


def needs_initiation(order: Order) -> bool:
    """True for a confirmed Ship Next order whose delivery has not been initiated successfully."""
    if order.delivery_partner != "shipnext" or order.order_status in NOT_DISPATCHABLE_STATUSES:
        return False
    shipment = getattr(order, "shipnext_shipment", None)
    return shipment is None or not shipment.is_sent


def _reason(body: Any) -> str:
    """What Ship Next said went wrong, in plain words (its message and any field errors)."""
    if isinstance(body, dict):
        parts = [str(body[key]) for key in ("message", "error", "errors") if body.get(key)]
        if parts:
            return "; ".join(parts)
    return str(body)


def order_weight_grams(order: Order) -> int:
    """Total weight in grams: product weight x quantity over all lines, 500 g when none is known."""
    total = sum(line_weight_grams(item.product, item.quantity) for item in order.items.select_related("product"))
    return total or DEFAULT_WEIGHT_GRAMS


def _line_sku(item: Any) -> str:
    """
    The code Ship Next shows for a line: the variant's SKU, or the product's SKU when the line has
    no variant or the variant has none. ``sku_suffix`` already holds the variant's complete code
    (it is not appended to the product SKU), and "0" is the catalog's placeholder for "not set".
    """
    variant_sku = (item.variant.sku_suffix or "").strip() if item.variant else ""
    return variant_sku if variant_sku and variant_sku != "0" else item.product.sku


def build_payload(order: Order) -> dict[str, Any]:
    """
    The JSON body Ship Next expects (Shopify-style: ``name`` is the order number and the
    delivery details are nested in ``shipping_address``).

    Raises:
        ShipNextError: the delivery details are missing or malformed (not retryable).
    """
    try:
        recipient = resolve_recipient(order)
    except IndiaPostBookingError as exc:
        raise ShipNextError(str(exc)) from exc
    length, breadth, height = box_dimensions(item.product for item in order.items.select_related("product"))

    return {
        "name": order.order_number,
        "total_price": float(order.total_amount),
        "total_weight": order_weight_grams(order),
        "pkg_length": length,
        "pkg_breadth": breadth,
        "pkg_height": height,
        #everything we sell ships in a box; "rol" (a cylinder: length + diameter) is never used
        "pkg_shape": PACKAGE_SHAPE_BOX,
        "financial_status": FINANCIAL_STATUS_COD if is_cod_order(order) else FINANCIAL_STATUS_PREPAID,
        "shipping_address": {
            "name": recipient.name,
            "phone": recipient.mobile,
            "address1": recipient.line1,
            "address2": recipient.line2,
            "city": recipient.city,
            "province": recipient.state,
            "zip": recipient.pincode,
            "country": "India",
        },
        "line_items": [
            {
                "order_id": order.order_number,
                "name": _line_sku(item),
                "price": float(item.unit_price),
                "fulfillable_quantity": item.quantity,
            }
            for item in order.items.select_related("product", "variant")
        ],
    }


def _claim_shipment(order: Order) -> tuple[ShipNextShipment, bool]:
    """
    Take the send claim for ``order``; returns ``(shipment, already_sent)``.

    The order row is locked while the claim is written, so a Celery retry, a dashboard
    click and a duplicate task can never all send the same order. The slow HTTP call happens
    after the lock is released.
    """
    try:
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=order.pk)
            shipment, _created = ShipNextShipment.objects.select_for_update().get_or_create(order=order)
            if shipment.is_sent:
                return shipment, True
            if shipment.send_status == SendStatus.SENDING and timezone.now() - shipment.updated_at < CLAIM_STALE_AFTER:
                raise ShipNextError("This order is already being sent to Ship Next.", retryable=True)
            shipment.send_status = SendStatus.SENDING
            shipment.attempts += 1
            shipment.error_message = ""
            shipment.save(update_fields=["send_status", "attempts", "error_message", "updated_at"])
            return shipment, False
    except IntegrityError as exc:
        raise ShipNextError("This order is already being sent to Ship Next.", retryable=True) from exc


def _record_failure(shipment: ShipNextShipment, message: str, response: Any = None) -> None:
    shipment.send_status = SendStatus.FAILED
    shipment.error_message = message
    update_fields = ["send_status", "error_message", "updated_at"]
    if response is not None:
        shipment.raw_response = response
        update_fields.append("raw_response")
    shipment.save(update_fields=update_fields)


def _post(url: str, payload: dict[str, Any]) -> tuple[int, Any]:
    """POST ``payload``; returns ``(status_code, parsed_body)``. Network trouble is a retryable ShipNextError."""
    try:
        response = requests.post(
            url, json=payload, headers={"Accept": "application/json"}, timeout=REQUEST_TIMEOUT_SECONDS
        )
    except requests.RequestException as exc:
        raise ShipNextError(f"Could not reach Ship Next: {exc}", retryable=True) from exc
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:500]}
    return response.status_code, body


def send_order(order: Order) -> ShipNextShipment:
    """
    Send ``order`` to Ship Next. Idempotent: an order Ship Next already accepted is not re-sent.

    Raises:
        ShipNextError: not sent. The failure is recorded on the shipment (except for problems
            that stop us before a shipment exists) and ``retryable`` says if a retry can help.
    """
    url = webhook_url()
    if not url:
        raise ShipNextError("SHIPNEXT_WEBHOOK_URL is not set.")
    if order.order_status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        raise ShipNextError(f"Order {order.order_number} is {order.order_status}; it is not sent to a courier.")
    #never book one order with two couriers
    if getattr(getattr(order, "indiapost_shipment", None), "barcode_number", None) or getattr(
        getattr(order, "delhivery_shipment", None), "waybill_number", None
    ):
        raise ShipNextError(f"Order {order.order_number} is already booked with another courier.")

    shipment, already_sent = _claim_shipment(order)
    if already_sent:
        return shipment

    try:
        status_code, body = _post(url, build_payload(order))
        print("Payload:", build_payload(order))
        print("Request Body:", body)
        accepted = status_code in (200, 201) and isinstance(body, dict) and body.get("success") is True
        if not accepted:
            #5xx / 429: Ship Next is struggling, try again later. Other 4xx or success=false: our data is rejected
            raise ShipNextError(
                f"Ship Next rejected the order (HTTP {status_code}): {_reason(body)}",
                retryable=status_code >= 500 or status_code == 429,
                response=body,
            )
    except ShipNextError as exc:
        _record_failure(shipment, str(exc), exc.response)
        raise
    except Exception as exc:
        #a bug must never leave the claim dangling for the stale-claim window
        _record_failure(shipment, f"Unexpected error: {exc}")
        raise

    shipment.send_status = SendStatus.SENT
    shipment.external_order_id = str(body.get("order_id", ""))
    shipment.sent_at = timezone.now()
    shipment.raw_response = body
    shipment.error_message = ""
    update_fields = ["send_status", "external_order_id", "sent_at", "raw_response", "error_message", "updated_at"]
    #the booking answer carries the tracking number; never blank out one entered by hand or by a callback
    tracking = str(body.get("tracking_number") or "").strip()[:50]
    if tracking:
        shipment.tracking_number = tracking
        update_fields.append("tracking_number")
    shipment.save(update_fields=update_fields)
    if order.delivery_partner != "shipnext":
        order.delivery_partner = "shipnext"
        order.save(update_fields=["delivery_partner", "updated_at"])
    logger.info("Order %s sent to Ship Next (order_id %s).", order.order_number, shipment.external_order_id)
    return shipment


# --------------------------------------------------------------------------- callback from Ship Next

class CallbackError(Exception):
    """A Ship Next callback that cannot be applied; ``status_code`` is the HTTP answer."""

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def apply_callback(order_ref: str, data: dict[str, Any]) -> dict[str, Any]:
    """
    Apply a Ship Next update to one order: its tracking number and/or a new status.

    ``order_ref`` is the order number as sent to Ship Next, with or without the leading ``#``
    ("3001" and "#3001" are the same order). Only orders locked to Ship Next can be changed, so a
    callback can never move another courier's order. An order never moves backwards, a delivered
    order is never cancelled by a late return, and repeating an update changes nothing.

    Raises:
        CallbackError: unknown order (404), not a Ship Next order (409) or nothing usable in the
            body (400).
    """
    number = "#" + str(order_ref).strip().lstrip("#")
    order = Order.objects.filter(order_number=number).first()
    if order is None:
        raise CallbackError(f"Order {number} was not found.", 404)
    if order.get_delivery_partner() != "shipnext":
        raise CallbackError(f"Order {number} is not a Ship Next order.", 409)

    tracking = str(data.get("tracking_number") or data.get("tracking_id") or data.get("awb") or "").strip()[:50]
    raw_status = str(data.get("status") or "").strip()
    #a tracking number with no status means the parcel is booked: that is "fulfilled"
    status_key = normalize_event_name(raw_status) if raw_status else ("FULFILLED" if tracking else "")
    target = SHIPNEXT_TO_ORDER_STATUS.get(status_key)
    if raw_status and target is None:
        allowed = ", ".join(key.lower() for key in SHIPNEXT_TO_ORDER_STATUS)
        raise CallbackError(f"Unknown status '{raw_status}'. Use one of: {allowed}.", 400)
    if not tracking and not target:
        raise CallbackError("Send a status and/or a tracking_number.", 400)

    shipment, _created = ShipNextShipment.objects.get_or_create(order=order)
    changed = []
    if tracking and shipment.tracking_number != tracking:
        shipment.tracking_number = tracking
        changed.append("tracking_number")
    if not shipment.is_sent:
        #Ship Next is reporting on this order, so it certainly has it
        shipment.send_status = SendStatus.SENT
        shipment.sent_at = timezone.now()
        shipment.error_message = ""
        changed += ["send_status", "sent_at", "error_message"]
    if changed:
        shipment.save(update_fields=changed + ["updated_at"])

    note = _move_order(order, target, status_key, tracking) if target else ""
    order.refresh_from_db(fields=["order_status"])
    return {
        "order": order.order_number,
        "order_status": order.order_status,
        "tracking_number": shipment.tracking_number,
        "message": note or f"Order {order.order_number} updated: {order.get_order_status_display()}.",
    }


def _move_order(order: Order, target: str, status_key: str, tracking: str) -> str:
    """
    Move ``order`` to ``target`` unless that would go backwards or undo a closed order.

    Returns a note explaining why nothing changed, or "" when the order moved (or was already there).
    """
    is_return = target == OrderStatus.CANCELLED
    if order.order_status == target:
        return ""
    if is_return and order.order_status in CLOSED_ORDER_STATUSES:
        logger.warning("Ship Next: ignoring %s for %s order %s", status_key, order.order_status, order.order_number)
        return f"Order {order.order_number} is already {order.get_order_status_display()}; status not changed."
    if not is_return and order.order_status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        return f"Order {order.order_number} is {order.get_order_status_display()}; status not changed."
    if not is_return and ORDER_STATUS_RANK.get(target, 0) < ORDER_STATUS_RANK.get(order.order_status, 0):
        return f"Order {order.order_number} is already {order.get_order_status_display()}; status not changed."
    try:
        transition_order_status(
            order=order,
            new_status=target,
            force=True,
            actor=None,
            note=f"Ship Next: {status_key}" + (f" (tracking {tracking})" if tracking else ""),
        )
    except Exception:
        logger.exception("Could not move order %s to %s from Ship Next update %s", order.order_number, target, status_key)
        raise CallbackError("The order status could not be changed. Please try again.", 500)
    return ""
