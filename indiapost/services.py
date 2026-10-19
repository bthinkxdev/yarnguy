import logging
import re
from decimal import Decimal
from typing import Optional
from django.conf import settings
from django.core.files.base import ContentFile
from django.utils import timezone

from indiapost.client import IndiaPostClient, IndiaPostClientError
from indiapost.models import IndiaPostBarcodePool, IndiaPostShipment
from orders.models import Order, OrderStatus

logger = logging.getLogger(__name__)


def _sanitize_phone(phone_raw: str) -> str:
    """
    As per India Post specifications:
    - Must be exactly 10 numeric digits.
    - Must start with 6, 7, 8, or 9.
    Returns the clean 10-digit number, or empty string if invalid.
    """
    digits = re.sub(r"\D", "", str(phone_raw or ""))
    if len(digits) > 10:
        digits = digits[-10:]
    if len(digits) == 10 and digits[0] in "6789":
        return digits
    return ""


def _calculate_order_weight_and_dims(order: Order) -> tuple[int, int, int, int]:
    """
    Computes total physical weight (integer grams) and dimensions (cm).
    """
    total_weight_grams = 0
    max_l, max_w, max_h = 10, 10, 5

    items = order.items.select_related("product").all()
    for item in items:
        p = item.product
        qty = item.quantity or 1
        if hasattr(p, "weight") and p.weight:
            #weight stored in kg or grams
            w_val = float(p.weight)
            #if weight is < 50 assume kg, convert to grams
            grams = int(w_val * 1000 if w_val < 50 else w_val)
            total_weight_grams += grams * qty

        if hasattr(p, "length") and p.length and float(p.length) > max_l:
            max_l = int(float(p.length))
        if hasattr(p, "width") and p.width and float(p.width) > max_w:
            max_w = int(float(p.width))
        if hasattr(p, "height") and p.height and float(p.height) > max_h:
            max_h = int(float(p.height))

    if total_weight_grams <= 0:
        total_weight_grams = 500  #default 500g

    return total_weight_grams, max(max_l, 14), max(max_w, 9), max(max_h, 1)


def trigger_indiapost_shipment(order: Order, force: bool = False) -> bool:
    """
    Creates an India Post booking for the given order and generates the shipping label.
    """
    #guard: do not book cancelled or refunded orders
    if order.order_status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        logger.warning(
            f"trigger_indiapost_shipment: Cannot book cancelled/refunded Order {order.order_number}."
        )
        return False

    #mutual exclusivity guard: prevent booking if Delhivery waybill already exists
    if hasattr(order, "delhivery_shipment") and getattr(order.delhivery_shipment, "waybill_number", None):
        logger.warning(
            f"trigger_indiapost_shipment: Order {order.order_number} already has active Delhivery "
            f"waybill ({order.delhivery_shipment.waybill_number}). India Post booking aborted."
        )
        return False

    existing = IndiaPostShipment.objects.filter(order=order).first()
    if existing and existing.barcode_number and existing.tracking_status not in ("Failed", "Initiated") and not force:
        logger.info(f"India Post shipment already exists for order {order.order_number} ({existing.barcode_number})")
        return True

    shipment = existing or IndiaPostShipment(order=order)

    #1.Address resolution - Strictly validate without inventing fake address data
    addr = order.delivery_address_snapshot or {}
    recipient_name = (addr.get("name") or addr.get("recipient_name") or "").strip()[:80]
    if not recipient_name:
        recipient_name = (order.customer_profile.name if order.customer_profile else "Customer")[:80]

    line1 = (addr.get("line1") or "").strip()[:80]
    line2 = (addr.get("line2") or "").strip()[:80]
    city = (addr.get("city") or "").strip()[:80]
    state = (addr.get("state") or "").strip()[:80]
    raw_pin = str(addr.get("pin") or addr.get("pincode") or addr.get("postal_code") or "").strip()
    destination_pin = re.sub(r"\D", "", raw_pin)[:6]

    if len(destination_pin) != 6:
        msg = f"Booking failed: Destination pincode is missing or invalid ('{raw_pin}'). Must be exactly 6 digits."
        shipment.error_message = msg
        shipment.tracking_status = "Failed"
        shipment.save()
        logger.error(f"Cannot book Order {order.order_number}: {msg}")
        return False

    if not line1:
        msg = "Booking failed: Delivery address line 1 is required."
        shipment.error_message = msg
        shipment.tracking_status = "Failed"
        shipment.save()
        logger.error(f"Cannot book Order {order.order_number}: {msg}")
        return False

    if not city:
        msg = "Booking failed: Delivery city is required."
        shipment.error_message = msg
        shipment.tracking_status = "Failed"
        shipment.save()
        logger.error(f"Cannot book Order {order.order_number}: {msg}")
        return False

    sender_mobile = _sanitize_phone(getattr(settings, "INDIA_POST_SENDER_MOBILE", ""))
    receiver_mobile = _sanitize_phone(
        addr.get("phone") or (order.customer_profile.phone if order.customer_profile else "")
    )

    if not sender_mobile:
        msg = "Booking failed: INDIA_POST_SENDER_MOBILE is missing or invalid. Must be exactly 10 digits starting with 6, 7, 8, or 9."
        shipment.error_message = msg
        shipment.tracking_status = "Failed"
        shipment.save()
        logger.error(f"Cannot book Order {order.order_number}: {msg}")
        return False

    if not receiver_mobile:
        msg = "Booking failed: Customer mobile number is missing or invalid. India Post requires a 10-digit number starting with 6, 7, 8, or 9."
        shipment.error_message = msg
        shipment.tracking_status = "Failed"
        shipment.save()
        logger.error(f"Cannot book Order {order.order_number}: {msg}")
        return False

    #2.Package calculation (All commercial merchandise/yarn products are parcels, not documents)
    weight_grams, length, width, height = _calculate_order_weight_and_dims(order)
    article_type = "SP_INLAND_PARCEL"
    contract_id = getattr(settings, "INDIA_POST_CONTRACT_ID_SP", "41585456")
    shape = "NROL"

    customer_id = getattr(settings, "INDIA_POST_CUSTOMER_ID", "3000064781")
    barcode = shipment.barcode_number or IndiaPostBarcodePool.allocate_next_barcode()

    #3.Pickup vs Dropoff settings
    origin_pincode = getattr(settings, "INDIA_POST_ORIGIN_PINCODE", "683105")
    origin_office_id = getattr(settings, "INDIA_POST_ORIGIN_OFFICE_ID", 21260024)
    sender_name = getattr(settings, "INDIA_POST_SENDER_NAME", "YARN GUY")[:80]
    sender_add1 = getattr(settings, "INDIA_POST_SENDER_ADDRESS_LINE1", "Aluva - Perumbavoor KSRTC Road, valiyyaveettil building")[:80]
    sender_city = getattr(settings, "INDIA_POST_SENDER_CITY", "Aluva")[:80]
    sender_state = getattr(settings, "INDIA_POST_SENDER_STATE", "KERALA")[:80]

    #check payment method
    is_cod = hasattr(order, "payment_transactions") and order.payment_transactions.filter(gateway_key="cod").exists()

    article_payload = {
        "bulk_customer_id": str(customer_id),
        "contract_id": str(contract_id),
        "barcode_no": barcode,
        "pickup_or_dropoff": "dropoff",
        "pickup_dropoff_office_id": int(origin_office_id),
        "article_type": "SP ",
        "physical_weight": weight_grams,
        "shape_of_article": shape,
        "length": str(length),
        "breadth_diameter": str(width),
        "height": str(height),
        "priority_flag": "FALSE",
        "delivery_instruction": "",
        "delivery_slot": "",
        "instruction_rts": "RTS",
        "sender_name": sender_name,
        "sender_company": sender_name,
        "sender_add_line_1": sender_add1,
        "sender_add_line_2": "",
        "sender_city": sender_city,
        "sender_state": sender_state,
        "sender_pincode": str(origin_pincode),
        "sender_emailid": "",
        "sender_alt_contact": "",
        "sender_kyc": "",
        "sender_tax_reference": "",
        "receiver_name": recipient_name,
        "receiver_company": recipient_name,
        "receiver_add_line_1": line1,
        "receiver_add_line_2": line2,
        "receiver_city": city,
        "receiver_state": state,
        "receiver_pincode": destination_pin,
        "receiver_emailid": "",
        "receiver_alt_contact": "",
        "receiver_kyc": "",
        "receiver_tax_reference": "",
        "alt_address_flag": "FALSE",
        "pickup_address_flag": "FALSE",
        "drop_off_pincode": str(origin_pincode),
        "sender_mobile_no": sender_mobile,
        "receiver_mobile_no": receiver_mobile,
        "prepayment_code": "",
        "value_of_prepayment": 0,
        "codr_cod": "COD" if is_cod else "",
        "value_for_codr_cod": float(order.total_amount) if is_cod else 0,
        "insurance_type": "",
        "value_of_insurance": 0,
        "ack": "FALSE",
        "reg": "FALSE",
        "otp": "FALSE",
        "bulk_reference": str(order.order_number),
        "pickup_address_id": "",
        "pickup_addressee_name": "",
        "pickup_company_name": "",
        "pickup_address_line1": "",
        "pickup_address_line2": "",
        "pickup_address_line3": "",
        "pickup_city": "",
        "pickup_state": "",
        "pickup_pincode": "",
        "pickup_email_id": "",
        "pickup_alt_contact_no": "0",
        "pickup_mobile_no": "0",
        "pickup_schedule_slot": "",
        "pickup_schedule_date": "",
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

    client = IndiaPostClient()

    try:
        booking_resp = client.book_articles(customer_id=customer_id, articles=[article_payload])
        shipment.raw_booking_response = booking_resp

        error_articles = booking_resp.get("error_articles", [])
        is_success = bool(
            booking_resp.get("success")
            and not error_articles
            and booking_resp.get("summary", {}).get("success_count", 0) > 0
        )

        if is_success:
            #barcode is only permanently associated with shipment on verified success
            shipment.barcode_number = barcode
            shipment.article_type = article_type
            shipment.contract_id = contract_id
            shipment.batch_id = booking_resp.get("batch_id", "")
            shipment.mail_booking_dom_id = str(booking_resp.get("mail_booking_dom_id", ""))
            shipment.tracking_status = "Booked"
            shipment.error_message = ""

            summary = booking_resp.get("summary", {})
            shipment.calculated_tariff = Decimal(str(summary.get("total_tariff_amount", 0)))
            shipment.save()

            #generate shipping label PDF
            generate_and_save_shipping_label(shipment, client=client)
            if order.delivery_partner != "indiapost":
                order.delivery_partner = "indiapost"
                order.save(update_fields=["delivery_partner", "updated_at"])
            logger.info(f"Successfully booked India Post shipment {barcode} for Order {order.order_number}")
            return True
        else:
            errors = error_articles or booking_resp.get("errors") or booking_resp.get("message")
            shipment.error_message = f"Booking rejected: {errors}"
            shipment.tracking_status = "Failed"
            #do not keep the barcode on failure so it can be cleanly re-allocated or re-tried
            shipment.barcode_number = None
            shipment.save()
            logger.error(f"India Post booking error for Order {order.order_number}: {errors}")
            return False

    except IndiaPostClientError as err:
        shipment.error_message = str(err)
        shipment.tracking_status = "Failed"
        shipment.barcode_number = None
        shipment.save()
        logger.error(f"India Post API call failed for Order {order.order_number}: {err}")
        return False


def generate_and_save_shipping_label(shipment: IndiaPostShipment, client: Optional[IndiaPostClient] = None) -> bool:
    """
    Generates and saves the A6 printable PDF shipping label using real order dimensions and weights.
    """
    if not shipment.barcode_number:
        return False

    client = client or IndiaPostClient()
    order = shipment.order
    addr = order.delivery_address_snapshot or {}

    customer_id = getattr(settings, "INDIA_POST_CUSTOMER_ID", "3000064781")
    origin_pincode = getattr(settings, "INDIA_POST_ORIGIN_PINCODE", "683105")
    sender_name = getattr(settings, "INDIA_POST_SENDER_NAME", "YARN GUY")
    sender_mobile = _sanitize_phone(getattr(settings, "INDIA_POST_SENDER_MOBILE", ""))
    sender_add1 = getattr(settings, "INDIA_POST_SENDER_ADDRESS_LINE1", "Aluva - Perumbavoor KSRTC Road, valiyyaveettil building")
    sender_city = getattr(settings, "INDIA_POST_SENDER_CITY", "Aluva")
    sender_state = getattr(settings, "INDIA_POST_SENDER_STATE", "KERALA")

    weight_grams, length, width, height = _calculate_order_weight_and_dims(order)
    is_cod = hasattr(order, "payment_transactions") and order.payment_transactions.filter(gateway_key="cod").exists()

    label_payload = [{
        "customer_id": int(customer_id),
        "delivery_office_name": "",
        "destination_pin": str(addr.get("pin") or addr.get("pincode") or "")[:6],
        "booking_datetime": timezone.now().strftime("%d-%m-%Y %H:%M:%S"),
        "channel_type": "E",
        "user_type": "R",
        "user_id": int(customer_id),
        "barcode_no": shipment.barcode_number,
        "service_type": "SP",
        "booking_type": "COMMERCIAL",
        "article_length": str(length),
        "article_breadth": str(width),
        "article_height": str(height),
        "charged_weight": float(weight_grams),
        "physical_weight": float(weight_grams),
        "volumetric_weight": float(weight_grams),
        "insurance_flag": False,
        "insurance_value": 0,
        "recipient_name": (addr.get("name") or addr.get("recipient_name") or "Customer")[:80],
        "recipient_mobile": _sanitize_phone(addr.get("phone") or (order.customer_profile.phone if order.customer_profile else "")),
        "recipient_addressl1": (addr.get("line1") or "")[:80],
        "recipient_addressl2": (addr.get("line2") or "")[:80],
        "recipient_addressl3": "",
        "recipient_city": (addr.get("city") or "")[:80],
        "recipient_pin": str(addr.get("pin") or addr.get("pincode") or "")[:6],
        "recipient_state": (addr.get("state") or "")[:80],
        "sender_name": sender_name,
        "sender_mobile": sender_mobile,
        "sender_addressl1": sender_add1,
        "sender_addressl2": "",
        "sender_addressl3": "",
        "sender_city": sender_city,
        "sender_pin": str(origin_pincode),
        "sender_state": sender_state,
        "transmission_mode": "S",
        "payment_mode": "COD" if is_cod else "CO",
        "routing_data": "",
        "booking_office_name": "Booking Counter",
        "booking_office_pin": str(origin_pincode),
        "size": "A6",
        "total_amount": float(shipment.calculated_tariff or 0),
        "payment_status": "PP" if is_cod else "PC",
        "value_added_services": "",
        "identifier": "Domestic",
        "bkg_ref_id": shipment.mail_booking_dom_id or str(order.order_number),
        "priority": False,
        "registered_flag": False,
    }]

    try:
        pdf_bytes = client.generate_domestic_label(label_payload)
        filename = f"label_{shipment.barcode_number}.pdf"
        shipment.label_pdf.save(filename, ContentFile(pdf_bytes), save=True)
        #clear any prior label error if successful
        if shipment.error_message and "label generation failed" in shipment.error_message.lower():
            shipment.error_message = None
            shipment.save(update_fields=["error_message", "updated_at"])
        logger.info(f"Saved shipping label for {shipment.barcode_number}")
        return True
    except Exception as exc:
        err_text = f"Booking succeeded, but label generation failed: {exc}"
        logger.error(f"Failed to generate label for {shipment.barcode_number}: {exc}")
        shipment.error_message = err_text
        shipment.save(update_fields=["error_message", "updated_at"])
        return False
