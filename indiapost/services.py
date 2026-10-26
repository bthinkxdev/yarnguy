"""India Post booking, label and tracking business logic."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.validators import normalize_indian_mobile, normalize_pincode
from indiapost.client import (
    IndiaPostAPIError,
    IndiaPostClient,
    IndiaPostClientError,
)
from indiapost.models import (
    BarcodePoolError,
    IndiaPostBarcodePool,
    IndiaPostShipment,
    ShipmentStatus,
)
from orders.models import Order, OrderStatus
from orders.services import ORDER_STATUS_RANK, transition_order_status

logger = logging.getLogger(__name__)

#India Post article types and their limits (cm / grams), per the Speed Post booking specification.
#Articles up to 500 g that fit the document envelope are SP_INLAND_DOC; everything else is a parcel.
ARTICLE_TYPE_DOC = "SP_INLAND_DOC"
ARTICLE_TYPE_PARCEL = "SP_INLAND_PARCEL"
SHAPE_DOC = "DOC"
SHAPE_PARCEL = "NROL"  # square / rectangular parcel
DOC_MAX_GRAMS = 500
PARCEL_MAX_GRAMS = 35_000
DOC_LIMITS = {"length": (1, 42), "width": (1, 29), "height": (1, 2)}
PARCEL_LIMITS = {"length": (14, 150), "width": (9, 150), "height": (1, 150)}

#pickup slots India Post accepts for a PICKUP booking, mapped to the slot's start hour
PICKUP_SLOTS = {"10:00-13:00": 10, "13:00-16:00": 13}

#a BOOKING claim older than this is assumed to belong to a crashed worker
CLAIM_STALE_AFTER = timedelta(minutes=10)

#orders in these states take no further courier updates
CLOSED_ORDER_STATUSES = (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED)

#India Post event code -> internal OrderStatus. The codes are matched upper-cased; event names
#from the tracking API ("Item Received at Destination") are normalised to the same form.
INDIA_POST_TO_ORDER_STATUS = {
    "ITEM_BOOKED": OrderStatus.READY_TO_SHIP,
    "BOOKED": OrderStatus.READY_TO_SHIP,
    "ITEM_DISPATCHED": OrderStatus.IN_TRANSIT,
    "DISPATCHED": OrderStatus.IN_TRANSIT,
    "BAG_CLOSE": OrderStatus.IN_TRANSIT,
    "BAG_RECEIVED": OrderStatus.IN_TRANSIT,
    "ITEM_BAGGED": OrderStatus.IN_TRANSIT,
    "ITEM_RECEIVED": OrderStatus.IN_TRANSIT,
    "ITEM_RECEIVED_AT_DESTINATION": OrderStatus.IN_TRANSIT,
    "TAKEN_OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
    "OUT_FOR_DELIVERY": OrderStatus.OUT_FOR_DELIVERY,
    "DELIVERED": OrderStatus.DELIVERED,
    "ITEM_DELIVERED": OrderStatus.DELIVERED,
    "ITEM_RETURNED": OrderStatus.CANCELLED,
    "RETURNED": OrderStatus.CANCELLED,
    "RTO": OrderStatus.CANCELLED,
    "RTA": OrderStatus.CANCELLED,
}


class IndiaPostBookingError(Exception):
    """A booking could not be completed. ``retryable`` says whether trying again later can help."""

    def __init__(self, message: str, *, retryable: bool = False, response: Any = None):
        super().__init__(message)
        self.retryable = retryable
        self.response = response  # India Post's response body, when it sent one


# --------------------------------------------------------------------------- configuration

@dataclass(frozen=True)
class IndiaPostConfig:
    customer_id: str
    contract_id: str
    origin_pincode: str
    origin_office_id: int
    sender_name: str
    sender_mobile: str
    sender_address_line1: str
    sender_city: str
    sender_state: str


def load_config() -> IndiaPostConfig:
    """Booking settings as configured (values may be blank — see ``config_problems``)."""
    return IndiaPostConfig(
        customer_id=str(settings.INDIA_POST_CUSTOMER_ID or "").strip(),
        contract_id=str(settings.INDIA_POST_CONTRACT_ID_SP or "").strip(),
        origin_pincode=str(settings.INDIA_POST_ORIGIN_PINCODE or "").strip(),
        origin_office_id=int(settings.INDIA_POST_ORIGIN_OFFICE_ID or 0),
        sender_name=str(settings.INDIA_POST_SENDER_NAME or "").strip(),
        sender_mobile=normalize_indian_mobile(settings.INDIA_POST_SENDER_MOBILE),
        sender_address_line1=str(settings.INDIA_POST_SENDER_ADDRESS_LINE1 or "").strip(),
        sender_city=str(settings.INDIA_POST_SENDER_CITY or "").strip(),
        sender_state=str(settings.INDIA_POST_SENDER_STATE or "").strip(),
    )


def config_problems() -> list[str]:
    """Human-readable list of missing/invalid India Post settings; empty when ready to book."""
    config = load_config()
    problems = []
    for name in ("BASE_URL", "USERNAME", "PASSWORD"):
        if not getattr(settings, f"INDIA_POST_{name}", ""):
            problems.append(f"INDIA_POST_{name} is not set")
    if not config.customer_id.isdigit():
        problems.append("INDIA_POST_CUSTOMER_ID must be the numeric customer id")
    if not config.contract_id.isdigit():
        problems.append("INDIA_POST_CONTRACT_ID_SP must be the numeric Speed Post contract id")
    if not normalize_pincode(config.origin_pincode):
        problems.append("INDIA_POST_ORIGIN_PINCODE must be a valid 6-digit PIN")
    if config.origin_office_id <= 0:
        problems.append("INDIA_POST_ORIGIN_OFFICE_ID must be the booking office id")
    if not config.sender_mobile:
        problems.append("INDIA_POST_SENDER_MOBILE must be a valid 10-digit mobile (starts 6-9)")
    for label, value in (
        ("INDIA_POST_SENDER_NAME", config.sender_name),
        ("INDIA_POST_SENDER_ADDRESS_LINE1", config.sender_address_line1),
        ("INDIA_POST_SENDER_CITY", config.sender_city),
        ("INDIA_POST_SENDER_STATE", config.sender_state),
    ):
        if not value:
            problems.append(f"{label} is not set")
    return problems


# --------------------------------------------------------------------------- order -> parcel

@dataclass(frozen=True)
class Parcel:
    weight_grams: int
    length: int
    width: int
    height: int
    article_type: str
    shape: str


def _fits(limits: dict[str, tuple[int, int]], length: int, width: int, height: int) -> bool:
    return all(lo <= value <= hi for value, (lo, hi) in zip((length, width, height), (limits["length"], limits["width"], limits["height"])))


def plan_parcel(lines: Iterable[tuple[Any, int]]) -> Parcel:
    """
    Weight, dimensions and India Post article type for ``(product, quantity)`` lines.

    ``Product.weight`` is stored in kilograms; a missing weight falls back to 500 g. The box is
    the largest declared product dimensions, never below the parcel minimum of 14 x 9 x 1 cm.
    An article of at most 500 g whose box fits the document envelope (<= 42 x 29 x 2 cm) is
    booked as ``SP_INLAND_DOC``; anything else as ``SP_INLAND_PARCEL``.

    Raises:
        IndiaPostBookingError: over 35 kg, or a side over 150 cm (India Post's limits).
    """
    total_weight_grams = 0
    length, width, height = PARCEL_LIMITS["length"][0], PARCEL_LIMITS["width"][0], PARCEL_LIMITS["height"][0]

    for product, quantity in lines:
        if product.weight:
            total_weight_grams += int(Decimal(str(product.weight)) * 1000) * (quantity or 1)
        length = max(length, math.ceil(float(product.length or 0)))
        width = max(width, math.ceil(float(product.width or 0)))
        height = max(height, math.ceil(float(product.height or 0)))

    weight = total_weight_grams or DOC_MAX_GRAMS
    if weight > PARCEL_MAX_GRAMS:
        raise IndiaPostBookingError(f"Parcel weighs {weight} g; India Post's maximum is {PARCEL_MAX_GRAMS} g.")
    if not _fits(PARCEL_LIMITS, length, width, height):
        raise IndiaPostBookingError(
            f"Parcel size {length} x {width} x {height} cm exceeds India Post's limit of 150 cm per side."
        )

    if weight <= DOC_MAX_GRAMS and _fits(DOC_LIMITS, length, width, height):
        return Parcel(weight, length, width, height, ARTICLE_TYPE_DOC, SHAPE_DOC)
    return Parcel(weight, length, width, height, ARTICLE_TYPE_PARCEL, SHAPE_PARCEL)


def order_lines(order: Order) -> list[tuple[Any, int]]:
    return [(item.product, item.quantity) for item in order.items.select_related("product")]


def _min3(value: str) -> str:
    """India Post rejects name / address fields shorter than 3 characters."""
    return value if len(value) >= 3 else value.ljust(3, ".")


def _optional_line(value: str) -> str:
    """
    An optional address line: blank stays blank, anything else is 3-80 characters.

    """
    value = (value or "").strip()[:80]
    return _min3(value) if value else ""


@dataclass(frozen=True)
class Party:
    """A sender or receiver of an article."""

    name: str
    line1: str
    line2: str
    city: str
    state: str
    pincode: str
    mobile: str


def store_party(config: "IndiaPostConfig") -> Party:
    return Party(
        name=config.sender_name,
        line1=config.sender_address_line1,
        line2="",
        city=config.sender_city,
        state=config.sender_state,
        pincode=config.origin_pincode,
        mobile=config.sender_mobile,
    )


def resolve_recipient(order: Order) -> Party:
    """
    Delivery details for ``order`` — strictly validated, never padded with placeholder data.

    Raises:
        IndiaPostBookingError: a required field is missing or malformed (not retryable).
    """
    addr = order.delivery_address_snapshot or {}

    name = (addr.get("name") or addr.get("recipient_name") or "").strip()
    if not name and order.customer_profile_id:
        name = order.customer_profile.user.get_full_name().strip()
    name = (name or "Customer")[:80]

    line1 = (addr.get("line1") or "").strip()[:80]
    city = (addr.get("city") or "").strip()[:80]
    raw_pin = str(addr.get("pin") or addr.get("pincode") or addr.get("postal_code") or "").strip()
    pincode = normalize_pincode(raw_pin)
    phone_raw = addr.get("phone") or (order.customer_profile.phone if order.customer_profile_id else "")
    mobile = normalize_indian_mobile(phone_raw)

    if not pincode:
        raise IndiaPostBookingError(
            f"Destination PIN code is missing or invalid ('{raw_pin}'). It must be exactly 6 digits."
        )
    if not line1:
        raise IndiaPostBookingError("Delivery address line 1 is required.")
    if not city:
        raise IndiaPostBookingError("Delivery city is required.")
    if not mobile:
        raise IndiaPostBookingError(
            "Customer mobile number is missing or invalid. India Post needs a 10-digit number starting with 6-9."
        )

    return Party(
        name=name,
        line1=line1,
        line2=(addr.get("line2") or "").strip()[:80],
        city=city,
        state=(addr.get("state") or "").strip()[:80],
        pincode=pincode,
        mobile=mobile,
    )


def is_cod_order(order: Order) -> bool:
    return order.payment_transactions.filter(gateway_key="cod").exists()


@dataclass(frozen=True)
class PickupRequest:
    """India Post collects the article from the sender's address (a ``PICKUP`` booking)."""

    office_id: int
    day: date
    slot: str  # one of PICKUP_SLOTS

    @property
    def schedule_text(self) -> str:
        """``MM/DD/YYYY HH:MM:SS AM/PM`` — the format the booking API requires."""
        start = datetime.combine(self.day, time(PICKUP_SLOTS[self.slot]))
        return start.strftime("%m/%d/%Y %I:%M:%S %p")


def build_article_payload(
    *,
    barcode: str,
    config: IndiaPostConfig,
    parcel: Parcel,
    sender: Party,
    receiver: Party,
    reference: str,
    cod_amount: Optional[Decimal] = None,
    pickup: Optional[PickupRequest] = None,
) -> dict[str, Any]:
    """
    One article record for India Post's bulk booking endpoint.

    Forward shipments use the store as sender and drop the parcel off at the booking office.
    A ``pickup`` turns it into a PICKUP booking: India Post collects from ``sender`` (the
    customer, for a return) and the pickup fields are filled from the sender's address.
    """
    return {
        "bulk_customer_id": config.customer_id,
        "contract_id": config.contract_id,
        "barcode_no": barcode,
        "pickup_or_dropoff": "PICKUP" if pickup else "DROPOFF",
        "pickup_dropoff_office_id": pickup.office_id if pickup else config.origin_office_id,
        "article_type": parcel.article_type,
        "physical_weight": parcel.weight_grams,
        "shape_of_article": parcel.shape,
        "length": str(parcel.length),
        "breadth_diameter": str(parcel.width),
        "height": str(parcel.height),
        "priority_flag": "FALSE",
        "delivery_instruction": "",
        "delivery_slot": "",
        "instruction_rts": "RTS",
        "sender_name": _min3(sender.name[:80]),
        "sender_company": _min3(sender.name[:80]),
        "sender_add_line_1": _min3(sender.line1[:80]),
        "sender_add_line_2": _optional_line(sender.line2),
        "sender_city": _min3(sender.city[:80]),
        "sender_state": sender.state[:80],
        "sender_pincode": sender.pincode,
        "sender_emailid": "",
        "sender_alt_contact": "",
        "sender_kyc": "",
        "sender_tax_reference": "",
        "receiver_name": _min3(receiver.name[:80]),
        "receiver_company": _min3(receiver.name[:80]),
        "receiver_add_line_1": _min3(receiver.line1[:80]),
        "receiver_add_line_2": _optional_line(receiver.line2),
        "receiver_city": _min3(receiver.city[:80]),
        "receiver_state": receiver.state[:80],
        "receiver_pincode": receiver.pincode,
        "receiver_emailid": "",
        "receiver_alt_contact": "",
        "receiver_kyc": "",
        "receiver_tax_reference": "",
        "alt_address_flag": "FALSE",
        "pickup_address_flag": "TRUE" if pickup else "FALSE",
        "drop_off_pincode": "" if pickup else config.origin_pincode,
        "sender_mobile_no": sender.mobile,
        "receiver_mobile_no": receiver.mobile,
        "prepayment_code": "",
        "value_of_prepayment": 0,
        "codr_cod": "COD" if cod_amount is not None else "",
        "value_for_codr_cod": float(cod_amount) if cod_amount is not None else 0,
        "insurance_type": "",
        "value_of_insurance": 0,
        "ack": "FALSE",
        "reg": "FALSE",
        "otp": "FALSE",
        "bulk_reference": reference,
        "pickup_address_id": "",
        "pickup_addressee_name": _min3(sender.name[:80]) if pickup else "",
        "pickup_company_name": _min3(sender.name[:80]) if pickup else "",
        "pickup_address_line1": _min3(sender.line1[:80]) if pickup else "",
        "pickup_address_line2": _optional_line(sender.line2) if pickup else "",
        "pickup_address_line3": "",
        "pickup_city": _min3(sender.city[:80]) if pickup else "",
        "pickup_state": sender.state[:80] if pickup else "",
        "pickup_pincode": sender.pincode if pickup else "",
        "pickup_email_id": "",
        "pickup_alt_contact_no": "0",
        "pickup_mobile_no": sender.mobile if pickup else "0",
        "pickup_schedule_slot": pickup.slot if pickup else "",
        "pickup_schedule_date": pickup.schedule_text if pickup else "",
        "alt_addressee_name": "",
        "alt_company_name": "",
        "alt_address_line1": "",
        "alt_address_line2": "",
        "alt_address_line3": "",
        "alt_city": "",
        "alt_state": "",
        "alt_pincode": "",
        "alt_email_id": "",
        "alt_contact_no": "0",
        "alt_alternate_mobile_no": "",
    }


# --------------------------------------------------------------------------- booking

def _claim_shipment(order: Order) -> tuple[IndiaPostShipment, bool]:
    """
    Take the booking claim for ``order``; returns ``(shipment, already_booked)``.

    The order row is locked while the claim is written, so a Celery retry, an admin click
    and a duplicate task can never all submit a booking for the same order. The claim is a
    ``Booking`` status on the shipment; the slow HTTP call happens after the lock is released.
    """
    try:
        with transaction.atomic():
            Order.objects.select_for_update().get(pk=order.pk)
            shipment = IndiaPostShipment.objects.select_for_update().filter(order=order).first()
            if shipment is None:
                shipment = IndiaPostShipment.objects.create(order=order, tracking_status=ShipmentStatus.BOOKING)
                return shipment, False
            if shipment.is_booked:
                return shipment, True
            if (
                shipment.tracking_status == ShipmentStatus.BOOKING
                and timezone.now() - shipment.updated_at < CLAIM_STALE_AFTER
            ):
                raise IndiaPostBookingError(
                    "A booking for this order is already in progress.", retryable=True
                )
            shipment.tracking_status = ShipmentStatus.BOOKING
            shipment.error_message = ""
            shipment.save(update_fields=["tracking_status", "error_message", "updated_at"])
            return shipment, False
    except IntegrityError as exc:
        raise IndiaPostBookingError("A booking for this order is already in progress.", retryable=True) from exc


def _record_failure(shipment: IndiaPostShipment, message: str) -> None:
    """Leave the shipment retryable: status Failed, reason recorded, barcode reservation kept."""
    shipment.tracking_status = ShipmentStatus.FAILED
    shipment.error_message = message
    shipment.save(update_fields=["tracking_status", "error_message", "raw_booking_response", "updated_at"])


def _booking_outcome(response: dict[str, Any]) -> tuple[bool, Any]:
    """
    Interpret a bulk-booking response: ``(accepted, errors)``.

    Accepted means ``success`` is true, no article-level errors came back, and — when the
    response carries a success count — at least one article was accepted.
    """
    errors = response.get("error_articles") or response.get("errors")
    summary = response.get("summary") or {}
    accepted_count = summary.get("success_count", 1)
    accepted = bool(response.get("success")) and not errors and accepted_count > 0
    return accepted, errors or response.get("message")


def submit_article(client: IndiaPostClient, config: IndiaPostConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Send one article to the booking API and return the accepted response.

    Raises:
        IndiaPostBookingError: rejected (4xx or article-level errors; not retryable), or the
            API was unreachable / unavailable (retryable). ``response`` carries any API body.
    """
    try:
        response = client.book_articles(config.customer_id, [payload])
    except IndiaPostAPIError as exc:
        body = exc.body if isinstance(exc.body, dict) else {"error": str(exc.body)}
        raise IndiaPostBookingError(f"{exc} {exc.body or ''}".strip(), response=body) from exc
    except IndiaPostClientError as exc:
        raise IndiaPostBookingError(str(exc), retryable=exc.retryable) from exc

    accepted, errors = _booking_outcome(response)
    if not accepted:
        raise IndiaPostBookingError(f"India Post rejected the booking: {errors}", response=response)
    return response


@dataclass(frozen=True)
class ReverseBooking:
    barcode: str
    batch_id: str
    tariff: Decimal
    response: dict[str, Any]


def book_reverse_pickup(
    *,
    barcode: str,
    reference: str,
    pickup_from: Party,
    lines: Iterable[tuple[Any, int]],
    day: date,
    slot: str,
    client: Optional[IndiaPostClient] = None,
) -> ReverseBooking:
    """
    Ask India Post to collect a parcel from ``pickup_from`` (the customer) and deliver it to the store.

    This is a ``PICKUP`` booking with the customer as sender and the store as receiver. The
    caller reserves ``barcode`` (so a retry re-submits the same number) and keeps the result.

    Raises:
        IndiaPostBookingError: not configured, bad slot, no India Post office for the pickup PIN,
            or the booking was rejected (not retryable) / India Post unavailable (retryable).
    """
    if slot not in PICKUP_SLOTS:
        raise IndiaPostBookingError(f"Pickup slot must be one of {', '.join(PICKUP_SLOTS)}.")
    problems = config_problems()
    if problems:
        raise IndiaPostBookingError("India Post is not configured: " + "; ".join(problems) + ".")
    config = load_config()
    client = client or IndiaPostClient()

    try:
        office_id = client.get_eligible_office_id(pickup_from.pincode)
    except IndiaPostClientError as exc:
        raise IndiaPostBookingError(str(exc), retryable=exc.retryable) from exc
    if not office_id:
        raise IndiaPostBookingError(f"India Post has no delivery office for pickup PIN {pickup_from.pincode}.")

    payload = build_article_payload(
        barcode=barcode,
        config=config,
        parcel=plan_parcel(lines),
        sender=pickup_from,
        receiver=store_party(config),
        reference=reference,
        pickup=PickupRequest(office_id=office_id, day=day, slot=slot),
    )
    response = submit_article(client, config, payload)
    summary = response.get("summary") or {}
    return ReverseBooking(
        barcode=barcode,
        batch_id=str(response.get("batch_id", "")),
        tariff=Decimal(str(summary.get("total_tariff_amount", 0))),
        response=response,
    )


def book_shipment(order: Order, *, client: Optional[IndiaPostClient] = None) -> IndiaPostShipment:
    """
    Book ``order`` with India Post and store the barcode and label. Idempotent.

    Returns the shipment (already-booked orders are returned untouched, with no API call).

    Raises:
        IndiaPostBookingError: the order cannot be booked right now; ``retryable`` tells the
            caller whether a later attempt can succeed. The reason is also stored on the
            shipment's ``error_message``.
    """
    if order.order_status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        raise IndiaPostBookingError(f"Cannot book a {order.get_order_status_display().lower()} order.")
    delhivery = getattr(order, "delhivery_shipment", None)
    if delhivery is not None and delhivery.waybill_number:
        raise IndiaPostBookingError(
            f"Delhivery waybill {delhivery.waybill_number} is already active on this order."
        )

    shipment, already_booked = _claim_shipment(order)
    if already_booked:
        logger.info("Order %s already booked with India Post (%s).", order.order_number, shipment.barcode_number)
        return shipment

    try:
        _submit_booking(order, shipment, client or IndiaPostClient())
    except IndiaPostBookingError as exc:
        _record_failure(shipment, str(exc))
        logger.error("India Post booking failed for order %s: %s", order.order_number, exc)
        raise
    except Exception as exc:
        #never leave the claim dangling on a bug — the shipment must stay retryable
        _record_failure(shipment, f"Unexpected error: {exc}")
        logger.exception("Unexpected error booking order %s with India Post", order.order_number)
        raise IndiaPostBookingError(f"Unexpected error: {exc}", retryable=True) from exc
    return shipment


def _submit_booking(order: Order, shipment: IndiaPostShipment, client: IndiaPostClient) -> None:
    problems = config_problems()
    if problems:
        raise IndiaPostBookingError("India Post is not configured: " + "; ".join(problems) + ".")
    config = load_config()
    recipient = resolve_recipient(order)

    #the barcode is reserved on the shipment before the call and kept if the call fails, so a
    #retry re-submits the same number instead of consuming a new one from the pool
    if not shipment.barcode_number:
        try:
            shipment.barcode_number = IndiaPostBarcodePool.allocate_next_barcode()
        except BarcodePoolError as exc:
            raise IndiaPostBookingError(str(exc)) from exc
        shipment.save(update_fields=["barcode_number", "updated_at"])

    parcel = plan_parcel(order_lines(order))
    payload = build_article_payload(
        barcode=shipment.barcode_number,
        config=config,
        parcel=parcel,
        sender=store_party(config),
        receiver=recipient,
        reference=str(order.order_number),
        cod_amount=order.total_amount if is_cod_order(order) else None,
    )

    try:
        response = submit_article(client, config, payload)
    except IndiaPostBookingError as exc:
        if exc.response is not None:
            shipment.raw_booking_response = exc.response
        raise

    shipment.raw_booking_response = response
    summary = response.get("summary") or {}
    shipment.article_type = parcel.article_type
    shipment.contract_id = config.contract_id
    shipment.batch_id = response.get("batch_id", "")
    shipment.mail_booking_dom_id = str(response.get("mail_booking_dom_id", ""))
    shipment.calculated_tariff = Decimal(str(summary.get("total_tariff_amount", 0)))
    shipment.tracking_status = ShipmentStatus.BOOKED
    shipment.error_message = ""
    shipment.save()

    if order.delivery_partner != "indiapost":
        order.delivery_partner = "indiapost"
        order.save(update_fields=["delivery_partner", "updated_at"])
    logger.info("Booked India Post shipment %s for order %s", shipment.barcode_number, order.order_number)

    #a missing label never undoes a successful booking — it is recorded and can be regenerated
    generate_and_save_shipping_label(shipment, client=client)


def generate_and_save_shipping_label(
    shipment: IndiaPostShipment, client: Optional[IndiaPostClient] = None
) -> bool:
    """Generate the A6 label PDF from the real order data and attach it to the shipment."""
    if not shipment.barcode_number:
        return False

    order = shipment.order
    try:
        problems = config_problems()
        if problems:
            raise IndiaPostBookingError("India Post is not configured: " + "; ".join(problems) + ".")
        config = load_config()
        recipient = resolve_recipient(order)
        parcel = plan_parcel(order_lines(order))
        customer_id = int(config.customer_id)

        label_payload = [{
            "customer_id": customer_id,
            "delivery_office_name": "",
            "destination_pin": recipient.pincode,
            "booking_datetime": timezone.now().strftime("%d-%m-%Y %H:%M:%S"),
            "channel_type": "E",
            "user_type": "R",
            "user_id": customer_id,
            "barcode_no": shipment.barcode_number,
            "service_type": "SP",
            "booking_type": "COMMERCIAL",
            "article_length": str(parcel.length),
            "article_breadth": str(parcel.width),
            "article_height": str(parcel.height),
            "charged_weight": float(parcel.weight_grams),
            "physical_weight": float(parcel.weight_grams),
            "volumetric_weight": float(parcel.weight_grams),
            "insurance_flag": False,
            "insurance_value": 0,
            "recipient_name": recipient.name,
            "recipient_mobile": recipient.mobile,
            "recipient_addressl1": recipient.line1,
            "recipient_addressl2": recipient.line2,
            "recipient_addressl3": "",
            "recipient_city": recipient.city,
            "recipient_pin": recipient.pincode,
            "recipient_state": recipient.state,
            "sender_name": config.sender_name,
            "sender_mobile": config.sender_mobile,
            "sender_addressl1": config.sender_address_line1,
            "sender_addressl2": "",
            "sender_addressl3": "",
            "sender_city": config.sender_city,
            "sender_pin": config.origin_pincode,
            "sender_state": config.sender_state,
            "transmission_mode": "S",
            #the label API only accepts contract payment (CO) and payment completed (PC); the
            #customer's own payment method (COD / prepaid) is carried by the booking, not here
            "payment_mode": "CO",
            "routing_data": "",
            "booking_office_name": "Booking Counter",
            "booking_office_pin": config.origin_pincode,
            "size": "A6",
            "total_amount": float(shipment.calculated_tariff or 0),
            "payment_status": "PC",
            "value_added_services": "",
            "identifier": "Domestic",
            "bkg_ref_id": shipment.mail_booking_dom_id or str(order.order_number),
            "priority": False,
            "registered_flag": False,
        }]
        pdf_bytes = (client or IndiaPostClient()).generate_domestic_label(label_payload)
    except (IndiaPostBookingError, IndiaPostClientError) as exc:
        shipment.error_message = f"Booking succeeded, but label generation failed: {exc}"
        shipment.save(update_fields=["error_message", "updated_at"])
        logger.error("Label generation failed for %s: %s", shipment.barcode_number, exc)
        return False

    shipment.label_pdf.save(f"label_{shipment.barcode_number}.pdf", ContentFile(pdf_bytes), save=False)
    if shipment.error_message and "label generation failed" in shipment.error_message.lower():
        shipment.error_message = ""
    shipment.save(update_fields=["label_pdf", "error_message", "updated_at"])
    logger.info("Saved shipping label for %s", shipment.barcode_number)
    return True


# --------------------------------------------------------------------------- tracking

def normalize_event_name(name: str) -> str:
    """``"Item received at Destination"`` -> ``"ITEM_RECEIVED_AT_DESTINATION"`` (webhook codes pass through)."""
    return re.sub(r"[^A-Z0-9]+", "_", (name or "").upper()).strip("_")


def extract_tracking_items(data: Any) -> list[dict[str, Any]]:
    """
    One entry per article from a bulk-tracking response.

    The API answers ``{"success": true, "data": [{"booking_details": {...},
    "tracking_details": [<events>], "del_status": {...}}]}``. Each entry reports the latest
    event (``event_code`` / ``event_description``), whether the article is flagged
    return-to-sender (``rts``) and whether India Post says it is delivered.
    """
    records = data.get("data") if isinstance(data, dict) else data
    items = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        article = str((record.get("booking_details") or {}).get("article_number") or "").strip()
        events = [e for e in (record.get("tracking_details") or []) if isinstance(e, dict)]
        if not article or not events:
            continue
        latest = max(events, key=lambda e: str(e.get("date") or ""))
        delivered = str((record.get("del_status") or {}).get("del_status") or "").lower() == "delivered"
        items.append({
            "article_number": article,
            "event_code": "DELIVERED" if delivered else normalize_event_name(latest.get("event")),
            "event_description": latest.get("remarks") or latest.get("event") or "",
            "rts": bool(latest.get("rts")),
        })
    return items


def apply_tracking_event(
    shipment: IndiaPostShipment, event_code: str, event_desc: str, *, source: str, returning: bool = False
) -> bool:
    """
    Record one tracking event on the shipment and move the order forward.

    Shared by the webhook and the periodic sync so both behave identically. Returns False
    for a repeat of the last event. An order never moves backwards, a delivered order is
    never cancelled by a late return scan, and a courier return cancels only live orders.
    ``returning`` marks an article India Post is sending back to the sender (RTS).
    """
    event_code = normalize_event_name(event_code)
    event_desc = (event_desc or "")[:255]
    if shipment.last_event_code == event_code and shipment.last_event_description == event_desc:
        return False

    shipment.last_event_code = event_code
    shipment.last_event_description = event_desc
    shipment.last_status_at = timezone.now()
    shipment.tracking_status = event_code or shipment.tracking_status
    shipment.save(
        update_fields=[
            "last_event_code", "last_event_description", "last_status_at", "tracking_status", "updated_at",
        ]
    )

    target = OrderStatus.CANCELLED if returning else INDIA_POST_TO_ORDER_STATUS.get(event_code)
    order = shipment.order
    if not target or order is None:
        return True

    is_return = target == OrderStatus.CANCELLED
    if is_return and order.order_status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        logger.warning("India Post %s: ignoring %s for %s order %s", source, event_code, order.order_status, order.order_number)
        return True
    if not is_return and ORDER_STATUS_RANK.get(target, 0) < ORDER_STATUS_RANK.get(order.order_status, 0):
        return True

    try:
        transition_order_status(
            order=order,
            new_status=target,
            force=True,
            actor=None,
            note=f"India Post {source}: {event_code} ({event_desc})",
        )
    except Exception:
        logger.exception("Could not move order %s to %s from India Post event %s", order.order_number, target, event_code)
    return True
