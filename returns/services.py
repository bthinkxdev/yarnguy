"""Business rules for customer returns and replacements.

Lifecycle::

    requested --approve--> approved --initiate pickup (India Post only)--> pickup_scheduled --> picked_up
        |                      |                                                   |               |
        +--reject / cancel     +--------------- received (items back) <------------+---------------+
                                                     |
                                   refund (customer asked for money back)  |  replaced (asked for a replacement)

Approving and arranging the pickup are separate admin decisions. The dashboard offers *Initiate
pickup* only when the order was delivered by India Post; for other couriers the items are collected
outside the app and the admin simply marks them received.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable, Optional

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from catalog.exceptions import InsufficientStockError
from catalog.services import adjust_stock
from core.services import get_site_settings
from indiapost.models import BarcodePoolError, IndiaPostBarcodePool
from indiapost.services import (
    PICKUP_SLOTS,
    IndiaPostBookingError,
    book_reverse_pickup,
    resolve_recipient,
)
from orders.models import Order, OrderItem, OrderStatus
from returns.models import (
    RELEASED_STATUSES,
    ReturnItem,
    ReturnReason,
    ReturnRequest,
    ReturnResolution,
    ReturnStatus,
)

logger = logging.getLogger(__name__)

#which statuses may follow which — anything else is refused
ALLOWED_TRANSITIONS: dict[str, tuple[str, ...]] = {
    ReturnStatus.REQUESTED: (ReturnStatus.APPROVED, ReturnStatus.REJECTED, ReturnStatus.CANCELLED),
    ReturnStatus.APPROVED: (ReturnStatus.PICKUP_SCHEDULED, ReturnStatus.RECEIVED, ReturnStatus.REJECTED),
    ReturnStatus.PICKUP_SCHEDULED: (ReturnStatus.PICKED_UP, ReturnStatus.RECEIVED),
    ReturnStatus.PICKED_UP: (ReturnStatus.RECEIVED,),
    ReturnStatus.RECEIVED: (ReturnStatus.REFUNDED, ReturnStatus.REPLACED),
}


class ReturnError(Exception):
    """A return action was refused; the message is safe to show to the person who tried it."""


# --------------------------------------------------------------------------- eligibility

@dataclass(frozen=True)
class EligibleItem:
    order_item: OrderItem
    available: int  # quantity not already covered by an open or completed return


@dataclass(frozen=True)
class Eligibility:
    allowed: bool
    message: str
    deadline: Optional[date]
    items: list[EligibleItem]


def delivered_at(order: Order):
    """When the order was delivered (falls back to its last update if no history was recorded)."""
    entry = order.status_history.filter(to_status=OrderStatus.DELIVERED).order_by("-changed_at").first()
    return entry.changed_at if entry else order.updated_at


def _returned_quantity(order_item: OrderItem) -> int:
    total = (
        ReturnItem.objects.filter(order_item=order_item)
        .exclude(return_request__status__in=RELEASED_STATUSES)
        .aggregate(total=Sum("quantity"))["total"]
    )
    return total or 0


def get_eligibility(order: Order) -> Eligibility:
    """
    What the customer may return from ``order`` right now.

    Returns open once the order is delivered and stay open for ``return_window_days``
    (Site Settings; 0 switches returns off). The last day is inclusive, counted in the
    store's local time. Each product line can be returned up to the quantity bought, across
    all of its non-rejected, non-cancelled requests.
    """
    window = get_site_settings().return_window_days
    if window <= 0:
        return Eligibility(False, "Returns are not accepted at the moment.", None, [])
    if order.order_status != OrderStatus.DELIVERED:
        return Eligibility(False, "Returns open once your order has been delivered.", None, [])

    deadline = timezone.localtime(delivered_at(order)).date() + timedelta(days=window)
    if timezone.localdate() > deadline:
        return Eligibility(
            False, f"The {window}-day return window for this order ended on {deadline:%b %d, %Y}.", deadline, []
        )

    items = []
    for order_item in order.items.select_related("product", "variant"):
        available = order_item.quantity - _returned_quantity(order_item)
        if available > 0:
            items.append(EligibleItem(order_item, available))
    if not items:
        return Eligibility(False, "Every item in this order already has a return request.", deadline, [])
    return Eligibility(True, f"You can return items until {deadline:%b %d, %Y}.", deadline, items)


def carrier_for(order: Order) -> str:
    """The courier that delivered the order (falls back to the store's active courier)."""
    partner = order.get_delivery_partner()
    if partner in ("delhivery", "indiapost"):
        return partner
    return get_site_settings().shipping_carrier or "delhivery"


# --------------------------------------------------------------------------- notifications

def _customer_email(request: ReturnRequest) -> str:
    profile = request.customer_profile
    if profile is not None and profile.user.email:
        return profile.user.email
    return (request.order.delivery_address_snapshot or {}).get("email", "")


def _notify(request: ReturnRequest, *, subject: str, body: str, to_store: bool = False) -> None:
    """Email after the surrounding transaction commits; a mail failure never undoes a return action."""
    from notifications.services import send_email

    recipient = get_site_settings().order_notification_email if to_store else _customer_email(request)
    if not recipient:
        return

    def _send() -> None:
        try:
            send_email(email=recipient, subject=subject, message=body)
        except Exception:
            logger.exception("Could not send return email %r for %s", subject, request.return_number)

    transaction.on_commit(_send)


def _signoff() -> str:
    return f"\n\nBest regards,\nThe {get_site_settings().site_name} Team"


# --------------------------------------------------------------------------- customer actions

def create_return_request(
    *,
    order: Order,
    customer_profile,
    selections: Iterable[tuple[int, int, str]],
    resolution: str = ReturnResolution.REFUND,
    note: str = "",
) -> ReturnRequest:
    """
    Record a return or replacement request for ``selections`` = ``(order_item_id, quantity, reason)``.

    Everything is validated server-side against the current eligibility: the order must belong
    to the customer, be inside the return window, and each quantity must not exceed what is
    still returnable for that product line.

    Raises:
        ReturnError: with a message for the customer.
    """
    if order.customer_profile_id != getattr(customer_profile, "pk", None):
        raise ReturnError("This order does not belong to your account.")
    if resolution not in ReturnResolution.values:
        raise ReturnError("Choose whether you want a refund or a replacement.")
    selections = [s for s in selections if s[1]]
    if not selections:
        raise ReturnError("Choose at least one item to return.")

    with transaction.atomic():
        Order.objects.select_for_update().get(pk=order.pk)  # serialise requests for one order
        eligibility = get_eligibility(order)
        if not eligibility.allowed:
            raise ReturnError(eligibility.message)
        available = {e.order_item.pk: e.available for e in eligibility.items}

        seen: set[int] = set()
        for item_id, quantity, reason in selections:
            if item_id in seen:
                raise ReturnError("Each product can only be listed once per request.")
            seen.add(item_id)
            if item_id not in available:
                raise ReturnError("One of the selected items cannot be returned.")
            if not 1 <= quantity <= available[item_id]:
                raise ReturnError(f"You can return at most {available[item_id]} of this item.")
            if reason not in ReturnReason.values:
                raise ReturnError("Choose a reason for each item you are returning.")

        sequence = order.return_requests.count() + 1
        request = ReturnRequest.objects.create(
            return_number=f"RET-{order.order_number.lstrip('#')}-{sequence}",
            order=order,
            customer_profile=customer_profile,
            resolution=resolution,
            carrier=carrier_for(order),
            customer_note=note.strip()[:1000],
        )
        by_id = {i.pk: i for i in order.items.all()}
        ReturnItem.objects.bulk_create(
            ReturnItem(return_request=request, order_item=by_id[item_id], quantity=quantity, reason=reason)
            for item_id, quantity, reason in selections
        )

        wanted = "a replacement" if resolution == ReturnResolution.REPLACEMENT else "a refund"
        _notify(
            request,
            subject=f"We received your request {request.return_number}",
            body=(
                f"Hi,\n\nWe have received your request {request.return_number} for {wanted} on order "
                f"{order.order_number}. We will review it and email you the outcome."
            )
            + _signoff(),
        )
        _notify(
            request,
            subject=f"New return request {request.return_number}",
            body=f"Order {order.order_number}: {len(selections)} item line(s), customer wants {wanted}. Review it in the dashboard.",
            to_store=True,
        )
    return request


def cancel_return_request(request: ReturnRequest, *, customer_profile) -> ReturnRequest:
    """The customer withdraws a request that has not been approved yet."""
    if request.customer_profile_id != getattr(customer_profile, "pk", None):
        raise ReturnError("This return does not belong to your account.")
    if request.status != ReturnStatus.REQUESTED:
        raise ReturnError("Only a request that has not been approved yet can be cancelled.")
    return _transition(request, ReturnStatus.CANCELLED)


# --------------------------------------------------------------------------- admin actions

def _transition(request: ReturnRequest, new_status: str, **fields) -> ReturnRequest:
    """Move to ``new_status`` if the lifecycle allows it, row-locked so two clicks can't both win."""
    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=request.pk)
        if new_status not in ALLOWED_TRANSITIONS.get(locked.status, ()):
            raise ReturnError(
                f"Cannot move a return from '{locked.get_status_display()}' to '{ReturnStatus(new_status).label}'."
            )
        locked.status = new_status
        for name, value in fields.items():
            setattr(locked, name, value)
        locked.save(update_fields=["status", "updated_at", *fields])
    request.refresh_from_db()
    return request


def approve_return(request: ReturnRequest) -> ReturnRequest:
    """
    Agree to the return. Nothing is collected yet: for India Post orders the admin then initiates
    the pickup; for other couriers the customer is told how to send the items back.
    """
    request = _transition(request, ReturnStatus.APPROVED, approved_at=timezone.now())
    if request.pickup_is_app_managed:
        next_step = "We will arrange a pickup from your delivery address and email you the date and tracking details."
    else:
        next_step = "Our team will contact you shortly with instructions for sending the items back."
    outcome = "replacement" if request.resolution == ReturnResolution.REPLACEMENT else "refund"
    _notify(
        request,
        subject=f"Your return {request.return_number} is approved",
        body=f"Good news - we have approved return {request.return_number}. Once we receive the items we will process your {outcome}.\n\n{next_step}"
        + _signoff(),
    )
    return request


def reject_return(request: ReturnRequest, *, reason: str) -> ReturnRequest:
    reason = reason.strip()
    if not reason:
        raise ReturnError("Give the customer a reason for rejecting the return.")
    request = _transition(request, ReturnStatus.REJECTED, admin_note=reason[:1000])
    _notify(
        request,
        subject=f"Update on your return request {request.return_number}",
        body=f"We are unable to accept return request {request.return_number}.\n\nReason: {reason}" + _signoff(),
    )
    return request


def initiate_pickup(request: ReturnRequest, *, pickup_day: date, pickup_slot: str) -> ReturnRequest:
    """
    Book the India Post reverse pickup for an approved return. Idempotent once scheduled.

    Only available when India Post delivered the order. A booking failure does not raise: it is
    stored on ``pickup_error`` (the return stays approved) so the admin can fix and retry.

    Raises:
        ReturnError: not an India Post order, wrong status, or an invalid date/slot.
    """
    if not request.pickup_is_app_managed:
        raise ReturnError("Pickups are only booked from here for orders delivered by India Post.")
    if pickup_day < timezone.localdate():
        raise ReturnError("The pickup date cannot be in the past.")
    if pickup_slot not in PICKUP_SLOTS:
        raise ReturnError("Choose a pickup time slot.")

    with transaction.atomic():
        locked = ReturnRequest.objects.select_for_update().get(pk=request.pk)
        if locked.status == ReturnStatus.PICKUP_SCHEDULED:
            return locked
        if locked.status != ReturnStatus.APPROVED:
            raise ReturnError("A pickup can only be initiated for an approved return.")
        locked.pickup_day, locked.pickup_slot = pickup_day, pickup_slot
        locked.save(update_fields=["pickup_day", "pickup_slot", "updated_at"])

    try:
        tracking, tariff, response = _book_india_post_pickup(locked)
    except IndiaPostBookingError as exc:
        ReturnRequest.objects.filter(pk=locked.pk).update(
            pickup_error=str(exc), pickup_response=exc.response or locked.pickup_response, updated_at=timezone.now()
        )
        logger.error("Reverse pickup failed for %s: %s", locked.return_number, exc)
        locked.refresh_from_db()
        return locked

    locked = _transition(
        locked,
        ReturnStatus.PICKUP_SCHEDULED,
        reverse_tracking_number=tracking,
        pickup_tariff=tariff,
        pickup_response=response,
        pickup_error="",
    )
    _notify_pickup_scheduled(locked)
    return locked


def _book_india_post_pickup(request: ReturnRequest) -> tuple[str, Decimal, dict]:
    lines = [(item.order_item.product, item.quantity) for item in request.items.select_related("order_item__product")]
    pickup_from = resolve_recipient(request.order)  # the validated delivery address, where the customer is

    if not request.reverse_tracking_number:
        #reserved on the request before the call and kept on failure, so a retry reuses the number
        try:
            ReturnRequest.objects.filter(pk=request.pk).update(
                reverse_tracking_number=IndiaPostBarcodePool.allocate_next_barcode()
            )
        except BarcodePoolError as exc:
            raise IndiaPostBookingError(str(exc)) from exc
        request.refresh_from_db(fields=["reverse_tracking_number"])

    booking = book_reverse_pickup(
        barcode=request.reverse_tracking_number,
        reference=request.return_number,
        pickup_from=pickup_from,
        lines=lines,
        day=request.pickup_day,
        slot=request.pickup_slot,
    )
    return booking.barcode, booking.tariff, booking.response


def _notify_pickup_scheduled(request: ReturnRequest) -> None:
    when = f"{request.pickup_day:%b %d, %Y}" + (f" between {request.pickup_slot}" if request.pickup_slot else "")
    _notify(
        request,
        subject=f"Pickup scheduled for your return {request.return_number}",
        body=(
            f"India Post will collect your return {request.return_number} from your delivery address on {when}.\n\n"
            f"Tracking number: {request.reverse_tracking_number}\n\n"
            "Please pack the items securely in their original packaging and keep them ready."
        )
        + _signoff(),
    )


def enter_pickup_manually(request: ReturnRequest, *, tracking_number: str, pickup_day: Optional[date] = None) -> ReturnRequest:
    """Fallback when the India Post API cannot book: the admin arranged the pickup elsewhere."""
    if not request.pickup_is_app_managed:
        raise ReturnError("Manual pickup details are only used for India Post returns.")
    tracking_number = tracking_number.strip()
    if not tracking_number:
        raise ReturnError("Enter the tracking number.")
    request = _transition(
        request,
        ReturnStatus.PICKUP_SCHEDULED,
        reverse_tracking_number=tracking_number,
        pickup_day=pickup_day or request.pickup_day,
        pickup_error="",
    )
    _notify_pickup_scheduled(request)
    return request


def mark_picked_up(request: ReturnRequest) -> ReturnRequest:
    return _transition(request, ReturnStatus.PICKED_UP, picked_up_at=timezone.now())


def mark_received(request: ReturnRequest, *, resellable: Iterable[int]) -> ReturnRequest:
    """
    The items reached the warehouse. ``resellable`` lists the ReturnItem ids in good condition:
    those go back into stock, the rest are recorded as not restocked.
    """
    resellable = set(resellable)
    with transaction.atomic():
        request = _transition(request, ReturnStatus.RECEIVED, received_at=timezone.now())
        for item in request.items.select_related("order_item__product", "order_item__variant"):
            item.restock = item.pk in resellable
            item.save(update_fields=["restock", "updated_at"])
            if item.restock:
                target = item.order_item.variant or item.order_item.product
                adjust_stock(target=target, delta=item.quantity, reason=f"return:{request.return_number}")
    outcome = "Your replacement is being prepared." if request.resolution == ReturnResolution.REPLACEMENT else "Your refund is being processed."
    _notify(
        request,
        subject=f"We received your return {request.return_number}",
        body=f"We have received the items from {request.return_number}. {outcome}" + _signoff(),
    )
    return request


def mark_refunded(request: ReturnRequest, *, amount: Optional[Decimal] = None) -> ReturnRequest:
    """
    Record that the customer has been refunded.

    This only records the refund — the money itself is returned through the payment provider
    (e.g. the Razorpay dashboard); no gateway call is made here.
    """
    if request.resolution != ReturnResolution.REFUND:
        raise ReturnError("The customer asked for a replacement, not a refund.")
    amount = request.items_value if amount is None else Decimal(amount)
    if amount < 0:
        raise ReturnError("The refund amount cannot be negative.")
    request = _transition(request, ReturnStatus.REFUNDED, refunded_at=timezone.now(), refund_amount=amount)
    _notify(
        request,
        subject=f"Your refund for {request.return_number}",
        body=f"We have refunded {amount} for return {request.return_number}. It can take a few days to show in your account."
        + _signoff(),
    )
    return request


def mark_replaced(request: ReturnRequest, *, tracking_number: str = "") -> ReturnRequest:
    """
    Record that a replacement has been sent. The same products and quantities leave stock, all or
    nothing: if any line is out of stock nothing changes and the admin is told which one.
    """
    if request.resolution != ReturnResolution.REPLACEMENT:
        raise ReturnError("The customer asked for a refund, not a replacement.")
    try:
        with transaction.atomic():
            request = _transition(
                request, ReturnStatus.REPLACED, replaced_at=timezone.now(), replacement_tracking=tracking_number.strip()[:100]
            )
            for item in request.items.select_related("order_item__product", "order_item__variant"):
                target = item.order_item.variant or item.order_item.product
                adjust_stock(target=target, delta=-item.quantity, reason=f"replacement:{request.return_number}")
    except InsufficientStockError as exc:
        request.refresh_from_db()
        raise ReturnError(f"Not enough stock to send the replacement. {exc}") from exc
    tracking = f"\n\nTracking number: {request.replacement_tracking}" if request.replacement_tracking else ""
    _notify(
        request,
        subject=f"Your replacement for {request.return_number} is on its way",
        body=f"We have sent the replacement for return {request.return_number}.{tracking}" + _signoff(),
    )
    return request
