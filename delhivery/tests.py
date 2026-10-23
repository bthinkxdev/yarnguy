"""Tests for the Delhivery integration: booking, address validation, webhook auth and status handling."""

from __future__ import annotations

import io
import json
import urllib.error
from contextlib import redirect_stdout
from decimal import Decimal
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs

from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from core.models import Currency
from delhivery.models import DelhiveryShipment
from delhivery.services import trigger_shipment_on_confirmed
from indiapost.models import IndiaPostShipment, ShipmentStatus
from orders.models import Order, OrderItem, OrderStatus
from payments.models import PaymentStatus, PaymentTransaction

DELHIVERY_CFG = dict(
    DELHIVERY_API_KEY="dl-key",
    DELHIVERY_BASE_URL="https://staging-express.delhivery.com",
    DELHIVERY_PICKUP_LOCATION="Warehouse-1",
    DELHIVERY_WEBHOOK_TOKEN="hook-token",
)


def fake_response(body):
    response = MagicMock()
    response.read.return_value = (json.dumps(body) if not isinstance(body, str) else body).encode()
    response.__enter__ = lambda self: self
    response.__exit__ = lambda *args: False
    return response


GOOD = {"success": True, "packages": [{"waybill": "DL1234567890"}]}


@override_settings(**DELHIVERY_CFG)
class DelhiveryTestCase(TestCase):
    seq = 0

    def setUp(self):
        self.currency, _ = Currency.objects.get_or_create(
            code="INR", defaults={"symbol": "₹", "exchange_rate_to_base": "1", "is_default": True}
        )
        category = Category.objects.create(name="Gym", slug="gym")
        self.hoodie = Product.objects.create(
            name="henley half sleeve", slug="hen", sku="HEN-1", category=category, base_price="500", mrp="500",
            purchase_price="300", stock_quantity=10, weight="0.400", length="25", width="20", height="3",
        )

    def make_order(self, *, cod=False, address=None, status=OrderStatus.CONFIRMED):
        DelhiveryTestCase.seq += 1
        order = Order.objects.create(
            order_number=f"#D{DelhiveryTestCase.seq}",
            order_status=status,
            success_at=timezone.now(),
            subtotal=Decimal("1000"),
            total_amount=Decimal("1050") if cod else Decimal("1000"),
            currency=self.currency,
            delivery_address_snapshot=address if address is not None else {
                "name": "Ravi K", "phone": "9876543210", "line1": "12 MG Road", "line2": "Aluva",
                "city": "Kochi", "state": "Kerala", "pincode": "682001",
            },
        )
        OrderItem.objects.create(order=order, product=self.hoodie, quantity=2, unit_price=Decimal("500"))
        PaymentTransaction.objects.create(
            order=order, gateway_key="cod" if cod else "razorpay_upi", amount=order.total_amount,
            currency=self.currency, status=PaymentStatus.PENDING if cod else PaymentStatus.SUCCESS,
        )
        return order

    def book(self, order, response=GOOD):
        with patch("urllib.request.urlopen", return_value=fake_response(response)) as urlopen:
            result = trigger_shipment_on_confirmed(order)
        return result, urlopen

    @staticmethod
    def sent_payload(urlopen):
        request = urlopen.call_args.args[0]
        return json.loads(parse_qs(request.data.decode())["data"][0]), request


# ============================================================================ booking

class BookingTests(DelhiveryTestCase):
    def test_prepaid_booking_payload_and_result(self):
        order = self.make_order()
        result, urlopen = self.book(order)
        payload, request = self.sent_payload(urlopen)
        shipment = payload["shipments"][0]
        self.assertTrue(result)
        self.assertEqual(request.full_url, "https://staging-express.delhivery.com/api/cmu/create.json")
        self.assertEqual(request.headers["Authorization"], "Token dl-key")
        self.assertEqual(payload["pickup_location"], {"name": "Warehouse-1"})
        self.assertEqual((shipment["payment_mode"], shipment["cod_amount"], shipment["total_amount"]), ("Prepaid", 0.0, 1000.0))
        self.assertEqual((shipment["pin"], shipment["city"], shipment["phone"]), ("682001", "Kochi", "9876543210"))
        self.assertEqual(shipment["order"], order.order_number)
        self.assertEqual(shipment["weight"], 800.0)  # 2 x 0.4 kg, in grams
        self.assertIn("Henley half sleeve", shipment["products_desc"])
        saved = DelhiveryShipment.objects.get(order=order)
        self.assertEqual((saved.waybill_number, saved.tracking_status, saved.pickup_location), ("DL1234567890", "Initiated", "Warehouse-1"))
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "delhivery")

    def test_cod_booking_collects_the_order_total(self):
        _, urlopen = self.book(self.make_order(cod=True))
        shipment = self.sent_payload(urlopen)[0]["shipments"][0]
        self.assertEqual((shipment["payment_mode"], shipment["cod_amount"]), ("COD", 1050.0))

    def test_missing_product_weight_falls_back_to_500_grams(self):
        Product.objects.filter(pk=self.hoodie.pk).update(weight=None)
        _, urlopen = self.book(self.make_order())
        self.assertEqual(self.sent_payload(urlopen)[0]["shipments"][0]["weight"], 500.0)

    def test_booking_twice_sends_once(self):
        order = self.make_order()
        self.book(order)
        result, urlopen = self.book(order)
        self.assertTrue(result)
        urlopen.assert_not_called()

    def test_an_order_already_booked_with_india_post_is_never_booked_with_delhivery(self):
        order = self.make_order()
        IndiaPostShipment.objects.create(order=order, barcode_number="ET214330016IN", tracking_status=ShipmentStatus.BOOKED)
        result, urlopen = self.book(order)
        self.assertFalse(result)
        urlopen.assert_not_called()

    @override_settings(DELHIVERY_API_KEY="")
    def test_missing_api_key_sends_nothing(self):
        result, urlopen = self.book(self.make_order())
        self.assertFalse(result)
        urlopen.assert_not_called()

    def test_no_customer_data_is_printed_to_the_console(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.book(self.make_order())
        self.assertEqual(buffer.getvalue(), "")


class AddressValidationTests(DelhiveryTestCase):
    def _book_with(self, **changes):
        address = {"name": "Ravi K", "phone": "9876543210", "line1": "12 MG Road", "city": "Kochi", "state": "Kerala", "pincode": "682001"}
        address.update(changes)
        order = self.make_order(address=address)
        result, urlopen = self.book(order)
        return order, result, urlopen

    def test_each_missing_or_invalid_field_is_refused_without_inventing_data(self):
        cases = {
            "pincode": ("", "PIN"), "pincode ": ("12345", "PIN"),
            "line1": ("", "address"), "city": ("", "city"), "phone": ("12345", "mobile"),
        }
        for field, (value, message) in cases.items():
            order, result, urlopen = self._book_with(**{field.strip(): value})
            self.assertFalse(result, field)
            urlopen.assert_not_called()
            shipment = DelhiveryShipment.objects.get(order=order)
            self.assertEqual(shipment.tracking_status, "Error")
            self.assertIn(message, shipment.error_message)

    def test_a_missing_order_phone_falls_back_to_the_customer_profile_number(self):
        from django.contrib.auth.models import User

        from accounts.services import ensure_customer_profile_for_user

        profile = ensure_customer_profile_for_user(user=User.objects.create_user("c", "c@example.com", "pw12345678"))
        profile.phone = "9123456789"
        profile.save(update_fields=["phone"])
        order = self.make_order(address={"name": "R", "line1": "a", "city": "Kochi", "pincode": "682001"})
        order.customer_profile = profile
        order.save()
        _, urlopen = self.book(order)
        self.assertEqual(self.sent_payload(urlopen)[0]["shipments"][0]["phone"], "9123456789")

    def test_international_style_numbers_are_normalised(self):
        _, _, urlopen = self._book_with(phone="+91 98765 43210")
        self.assertEqual(self.sent_payload(urlopen)[0]["shipments"][0]["phone"], "9876543210")


class ApiFailureTests(DelhiveryTestCase):
    def test_api_reported_error_is_stored(self):
        order = self.make_order()
        result, _ = self.book(order, {"success": False, "error": True, "rmk": "Pickup location invalid"})
        self.assertFalse(result)
        saved = DelhiveryShipment.objects.get(order=order)
        self.assertEqual(saved.tracking_status, "Error")
        self.assertIn("Pickup location invalid", saved.error_message)

    def test_http_error_is_stored(self):
        order = self.make_order()
        error = urllib.error.HTTPError("https://x", 500, "boom", {}, io.BytesIO(b"server exploded"))
        with patch("urllib.request.urlopen", side_effect=error):
            self.assertFalse(trigger_shipment_on_confirmed(order))
        saved = DelhiveryShipment.objects.get(order=order)
        self.assertEqual(saved.tracking_status, "HTTP Error 500")
        self.assertIn("server exploded", saved.error_message)

    def test_connection_failure_is_stored(self):
        order = self.make_order()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("no route")):
            self.assertFalse(trigger_shipment_on_confirmed(order))
        self.assertEqual(DelhiveryShipment.objects.get(order=order).tracking_status, "Connection Error")

    def test_a_failed_booking_can_be_retried_and_then_succeeds(self):
        order = self.make_order()
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("down")):
            trigger_shipment_on_confirmed(order)
        result, _ = self.book(order)
        self.assertTrue(result)
        self.assertEqual(DelhiveryShipment.objects.get(order=order).waybill_number, "DL1234567890")


# ============================================================================ webhook

class WebhookTests(DelhiveryTestCase):
    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.shipment = DelhiveryShipment.objects.create(order=self.order, waybill_number="DL1234567890", tracking_status="Initiated")

    def push(self, status, *, waybill="DL1234567890", when="2026-10-01T10:00:00", token="hook-token", flat=False):
        payload = (
            {"waybill": waybill, "status": status, "status_datetime": when}
            if flat else
            {"Shipment": {"AWB": waybill, "Status": {"Status": status, "StatusDateTime": when}, "ReferenceNo": self.order.order_number}}
        )
        headers = {"HTTP_X_DELHIVERY_TOKEN": token} if token is not None else {}
        return self.client.post(reverse("delhivery:webhook"), json.dumps(payload), content_type="application/json", **headers)

    def status(self):
        self.order.refresh_from_db()
        return self.order.order_status

    def test_authentication_fails_closed(self):
        self.assertEqual(self.push("Delivered", token=None).status_code, 401)
        self.assertEqual(self.push("Delivered", token="wrong").status_code, 401)
        with override_settings(DELHIVERY_WEBHOOK_TOKEN=""):
            self.assertEqual(self.push("Delivered").status_code, 503)
        self.assertEqual(self.status(), OrderStatus.CONFIRMED)

    def test_a_scan_progression_moves_the_order_forward(self):
        for index, (scan, expected) in enumerate([
            ("Manifested", OrderStatus.READY_TO_SHIP),
            ("Picked Up", OrderStatus.PICKED_UP),
            ("In Transit", OrderStatus.IN_TRANSIT),
            ("Out For Delivery", OrderStatus.OUT_FOR_DELIVERY),
            ("Delivered", OrderStatus.DELIVERED),
        ]):
            self.assertEqual(self.push(scan, when=f"2026-10-0{index + 1}T10:00:00").status_code, 200, scan)
            self.assertEqual(self.status(), expected, scan)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.tracking_status, "Delivered")

    def test_flat_payload_shape_is_accepted(self):
        self.assertEqual(self.push("In Transit", flat=True).status_code, 200)
        self.assertEqual(self.status(), OrderStatus.IN_TRANSIT)

    def test_repeated_scan_is_ignored(self):
        self.push("In Transit")
        self.assertEqual(json.loads(self.push("In Transit").content)["status"], "duplicate_ignored")

    def test_stale_scan_never_moves_the_order_backwards(self):
        self.push("Out For Delivery", when="2026-10-02T10:00:00")
        response = self.push("In Transit", when="2026-10-01T10:00:00")
        self.assertEqual(json.loads(response.content)["status"], "ignored_regression")
        self.assertEqual(self.status(), OrderStatus.OUT_FOR_DELIVERY)

    def test_return_to_origin_cancels_an_order_in_transit(self):
        self.push("In Transit", when="2026-10-01T10:00:00")
        self.push("RTO", when="2026-10-03T10:00:00")
        self.assertEqual(self.status(), OrderStatus.CANCELLED)

    def test_a_late_return_scan_never_cancels_a_delivered_order(self):
        self.push("Delivered", when="2026-10-01T10:00:00")
        response = self.push("RTO", when="2026-10-04T10:00:00")
        self.assertEqual(json.loads(response.content)["status"], "ignored_closed_order")
        self.assertEqual(self.status(), OrderStatus.DELIVERED)

    def test_unmapped_status_is_recorded_without_changing_the_order(self):
        self.push("Pending Customer Contact")
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.tracking_status, "Pending Customer Contact")
        self.assertEqual(self.status(), OrderStatus.CONFIRMED)

    def test_lookup_falls_back_to_the_order_number(self):
        response = self.push("In Transit", waybill="")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(), OrderStatus.IN_TRANSIT)

    def test_unknown_and_malformed_requests(self):
        unknown_order = self.client.post(
            reverse("delhivery:webhook"), json.dumps({"waybill": "NOPE", "status": "delivered"}), content_type="application/json",
            HTTP_X_DELHIVERY_TOKEN="hook-token",
        )
        self.assertEqual(unknown_order.status_code, 404)
        self.assertEqual(self.client.post(reverse("delhivery:webhook"), json.dumps({}), content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token").status_code, 400)
        self.assertEqual(self.client.post(reverse("delhivery:webhook"), "not json", content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token").status_code, 400)
        self.assertEqual(self.client.get(reverse("delhivery:webhook")).status_code, 405)

    def test_delhivery_validation_test_payload_is_acknowledged(self):
        response = self.push("Delivered", waybill="XXXXXXXXXXXX")
        self.assertEqual(json.loads(response.content)["status"], "test_payload_acknowledged")

    def test_both_webhook_url_forms_work(self):
        response = self.client.post("/delhivery/webhook", json.dumps({"waybill": "DL1234567890", "status": "in transit"}),
                                    content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token")
        self.assertEqual(response.status_code, 200)
