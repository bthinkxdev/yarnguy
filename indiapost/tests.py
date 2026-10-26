"""Tests for the India Post integration.

A fake India Post server (``FakeIndiaPost``) stands in for the network so every path —
success, expired token, 4xx rejection, 5xx outage, duplicate barcode — can be exercised.
The real system is exercised separately by ``manage.py indiapost_uat``.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from celery.exceptions import Retry
from django.conf import settings
from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.forms import AddressForm, CustomerProfileEditForm
from accounts.models import Address
from accounts.services import ensure_customer_profile_for_user
from catalog.models import Category, Product
from core.models import Currency, SiteSettings
from core.validators import normalize_indian_mobile, normalize_pincode
from indiapost import tasks as ip_tasks
from indiapost.client import (
    IndiaPostAPIError,
    IndiaPostAuthError,
    IndiaPostClient,
    IndiaPostConfigError,
    IndiaPostTransientError,
)
from indiapost.uat import ConnectionCheck, check_connection
from indiapost.models import (
    BarcodePoolError,
    IndiaPostBarcodePool,
    IndiaPostShipment,
    ShipmentStatus,
    calculate_upu_s10_check_digit,
)
from indiapost.services import (
    CLAIM_STALE_AFTER,
    IndiaPostBookingError,
    book_shipment,
    config_problems,
    generate_and_save_shipping_label,
)
from orders.admin import OrderAdmin
from orders.forms import OrderAddressForm
from orders.models import Order, OrderItem, OrderStatus
from orders.services import transition_order_status
from payments.models import PaymentStatus, PaymentTransaction

MEDIA = tempfile.mkdtemp()

CFG = dict(
    INDIA_POST_BASE_URL="https://test.cept.gov.in/beextcustomer",
    INDIA_POST_USERNAME="uat-user",
    INDIA_POST_PASSWORD="uat-pass",
    INDIA_POST_CUSTOMER_ID="3000000001",
    INDIA_POST_CONTRACT_ID_SP="41000001",
    INDIA_POST_CONTRACT_ID_BP="41000002",
    INDIA_POST_ORIGIN_PINCODE="683105",
    INDIA_POST_ORIGIN_OFFICE_ID=21000001,
    INDIA_POST_SENDER_NAME="YARN GUY",
    INDIA_POST_SENDER_MOBILE="9876501234",
    INDIA_POST_SENDER_ADDRESS_LINE1="Aluva - Perumbavoor KSRTC Road",
    INDIA_POST_SENDER_CITY="Aluva",
    INDIA_POST_SENDER_STATE="KERALA",
    INDIA_POST_WEBHOOK_TOKEN="hook-secret",
    MEDIA_ROOT=MEDIA,
)


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


class FakeResponse:
    def __init__(self, status=200, body=None, content=b""):
        self.status_code = status
        self._body = body
        self.content = content or (json.dumps(body).encode() if body is not None else b"")
        self.text = self.content.decode("utf-8", "ignore")
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class FakeIndiaPost:
    """Minimal stand-in for the India Post API; records every call."""

    def __init__(self):
        self.calls = []
        self.token = "tok-1"
        self.logins = 0
        self.login_ok = True
        self.booked = set()
        self.book_queue = []        # [(status, body)] consumed before default behaviour
        self.label_status = 200
        self.tracking_data = []
        self.on_book = None         # callable run inside a booking call

    def request(self, method, url, params=None, json=None, headers=None, timeout=None):
        #copy: the client reuses one headers dict across a retry, and each call must keep its own
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "headers": dict(headers or {})})
        if url.endswith("/v1/access/login"):
            self.logins += 1
            if not self.login_ok or json.get("password") != CFG["INDIA_POST_PASSWORD"]:
                return FakeResponse(401, {"success": False, "message": "Invalid credentials"})
            return FakeResponse(200, {"success": True, "data": {"access_token": self.token}})
        if (headers or {}).get("Authorization") != f"Bearer {self.token}":
            return FakeResponse(401, {"message": "token expired"})
        if url.endswith("/v1/pincode-search"):
            if str(params["pincode"]).startswith("0"):
                return FakeResponse(200, [])
            return FakeResponse(200, [{"office_id": 21000001, "delivery_office_flag": True, "office_type_code": "HO"}])
        if "tariff" in url:
            return FakeResponse(200, {"total_tariff_amount": 55})
        if "/process-articles/" in url:
            if self.on_book:
                self.on_book()
            if self.book_queue:
                status, body = self.book_queue.pop(0)
                return FakeResponse(status, body)
            article = json["articles"][0]
            if article["barcode_no"] in self.booked:
                return FakeResponse(400, {"success": False, "errors": ["duplicate barcode"]})
            if article["receiver_pincode"] == "999999":
                return FakeResponse(400, {"success": False, "errors": ["invalid destination pincode"]})
            self.booked.add(article["barcode_no"])
            return FakeResponse(200, {
                "success": True, "batch_id": "B1", "mail_booking_dom_id": 77,
                "summary": {"total_tariff_amount": 55.5, "success_count": 1},
            })
        if url.endswith("/v1/label/create/domestic"):
            if self.label_status != 200:
                return FakeResponse(self.label_status, {"message": "label error"})
            return FakeResponse(200, None, content=b"%PDF-1.4 fake label")
        if url.endswith("/v1/tracking/bulk"):
            return FakeResponse(200, {"status_code": 200, "success": True, "message": "data retrieved successfully", "data": self.tracking_data})
        return FakeResponse(404, {"message": "not found"})

    @staticmethod
    def track_record(barcode, event, *, remarks="", rts=False, delivered=False, date="2026-09-07T15:24:12Z"):
        """One article in the bulk-tracking response, in India Post's documented shape."""
        return {
            "booking_details": {"article_number": barcode, "article_type": "SP_INLAND_PARCEL"},
            "tracking_details": [
                {"date": date, "office": "Kalanjoor SO", "event": event, "remarks": remarks, "rts": rts},
                {"date": "2026-09-05T23:24:16Z", "office": "Changanacherry HO", "event": "Item Booked", "remarks": "", "rts": False},
            ],
            "del_status": {"del_status": "delivered" if delivered else "not delivered"},
        }

    def booking_calls(self):
        return [c for c in self.calls if "/process-articles/" in c["url"]]

    def tracking_calls(self):
        return [c for c in self.calls if c["url"].endswith("/v1/tracking/bulk")]


@override_settings(**CFG)
class IndiaPostTestCase(TestCase):
    seq = 0

    def setUp(self):
        cache.clear()
        self.net = FakeIndiaPost()
        patcher = patch("indiapost.client.requests.request", self.net.request)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.currency, _ = Currency.objects.get_or_create(
            code="INR", defaults={"symbol": "₹", "exchange_rate_to_base": "1", "is_default": True}
        )
        self.user = User.objects.create_user("buyer", "buyer@example.com", "pw12345678", first_name="Asha", last_name="Nair")
        self.profile = ensure_customer_profile_for_user(user=self.user)
        self.profile.phone = "9876543210"
        self.profile.save(update_fields=["phone"])
        category = Category.objects.create(name="Gym", slug="gym")
        self.product = Product.objects.create(
            name="Hoodie", slug="hoodie", sku="HOOD-1", category=category, base_price="500", mrp="500",
            purchase_price="300", stock_quantity=100, weight="0.400",
        )
        IndiaPostBarcodePool.objects.create(prefix="ET", current_number=21433001, end_number=21433010, suffix="IN")

    def make_order(self, *, cod=False, address=None, status=None):
        IndiaPostTestCase.seq += 1
        order = Order.objects.create(
            customer_profile=self.profile,
            order_number=f"#T{IndiaPostTestCase.seq}",
            order_status=status or (OrderStatus.PLACED_COD if cod else OrderStatus.CONFIRMED),
            success_at=timezone.now(),
            subtotal=Decimal("500"),
            total_amount=Decimal("550") if cod else Decimal("500"),
            currency=self.currency,
            delivery_address_snapshot=address if address is not None else {
                "name": "Ravi K", "phone": "9876543210", "line1": "12 MG Road", "line2": "",
                "city": "Kochi", "state": "Kerala", "pincode": "682001",
            },
        )
        OrderItem.objects.create(order=order, product=self.product, quantity=1, unit_price=Decimal("500"))
        PaymentTransaction.objects.create(
            order=order, gateway_key="cod" if cod else "razorpay_upi", amount=order.total_amount,
            currency=self.currency, status=PaymentStatus.PENDING if cod else PaymentStatus.SUCCESS,
        )
        return order

    def pool_used(self):
        return IndiaPostBarcodePool.objects.get().current_number - 21433001


# ============================================================================ booking core

class BookingTests(IndiaPostTestCase):
    def test_prepaid_booking_stores_barcode_label_and_tariff(self):
        order = self.make_order()
        shipment = book_shipment(order)
        shipment.refresh_from_db()
        self.assertEqual(shipment.tracking_status, ShipmentStatus.BOOKED)
        self.assertEqual(len(shipment.barcode_number), 13)
        self.assertEqual(shipment.batch_id, "B1")
        self.assertEqual(shipment.calculated_tariff, Decimal("55.50"))
        self.assertTrue(shipment.label_pdf)
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "indiapost")

    def test_cod_booking_sends_cod_flag_and_order_total(self):
        book_shipment(self.make_order(cod=True))
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual((article["codr_cod"], article["value_for_codr_cod"]), ("COD", 550.0))

    def test_payload_uses_real_parcel_data_and_clean_article_type(self):
        book_shipment(self.make_order())
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual(article["article_type"], "SP_INLAND_DOC")  # <= 500 g and fits the envelope
        self.assertEqual(article["physical_weight"], 400)
        self.assertEqual(article["shape_of_article"], "DOC")
        self.assertEqual(article["pickup_or_dropoff"], "DROPOFF")
        self.assertEqual(article["pickup_address_flag"], "FALSE")
        self.assertEqual(article["receiver_pincode"], "682001")
        self.assertEqual(article["sender_pincode"], "683105")

    def test_barcode_check_digit_is_valid_s10(self):
        self.assertEqual(calculate_upu_s10_check_digit(21433001), 6)

    def test_rejected_booking_is_recorded_and_retryable(self):
        order = self.make_order()
        self.net.book_queue.append((400, {"success": False, "errors": ["bad data"]}))
        with self.assertRaises(IndiaPostBookingError) as ctx:
            book_shipment(order)
        self.assertFalse(ctx.exception.retryable)
        shipment = IndiaPostShipment.objects.get(order=order)
        self.assertEqual(shipment.tracking_status, ShipmentStatus.FAILED)
        self.assertIn("bad data", shipment.error_message)
        book_shipment(order)  # a later attempt goes through
        self.assertEqual(IndiaPostShipment.objects.get(order=order).tracking_status, ShipmentStatus.BOOKED)

    def test_success_with_article_errors_is_not_a_booking(self):
        order = self.make_order()
        self.net.book_queue.append((200, {"success": True, "error_articles": [{"error": "invalid pin"}]}))
        with self.assertRaises(IndiaPostBookingError):
            book_shipment(order)
        self.assertEqual(IndiaPostShipment.objects.get(order=order).tracking_status, ShipmentStatus.FAILED)

    def test_success_response_without_a_summary_is_accepted(self):
        order = self.make_order()
        self.net.book_queue.append((200, {"success": True, "batch_id": "B9"}))
        self.assertEqual(book_shipment(order).tracking_status, ShipmentStatus.BOOKED)

    def test_server_error_is_retryable_client_error_is_not(self):
        order = self.make_order()
        self.net.book_queue.append((503, {"message": "down"}))
        with self.assertRaises(IndiaPostBookingError) as ctx:
            book_shipment(order)
        self.assertTrue(ctx.exception.retryable)

    def test_label_failure_never_undoes_a_booking(self):
        order = self.make_order()
        self.net.label_status = 500
        shipment = book_shipment(order)
        shipment.refresh_from_db()
        self.assertEqual(shipment.tracking_status, ShipmentStatus.BOOKED)
        self.assertIn("label generation failed", shipment.error_message)
        self.net.label_status = 200
        self.assertTrue(generate_and_save_shipping_label(shipment))
        shipment.refresh_from_db()
        self.assertTrue(shipment.label_pdf)
        self.assertEqual(shipment.error_message, "")

    def test_cancelled_and_refunded_orders_cannot_be_booked(self):
        for status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
            with self.assertRaises(IndiaPostBookingError):
                book_shipment(self.make_order(status=status))
        self.assertEqual(self.net.booking_calls(), [])

    def test_repeat_booking_is_idempotent(self):
        order = self.make_order()
        first = book_shipment(order)
        second = book_shipment(order)
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(len(self.net.booking_calls()), 1)


# ============================================================================ the 14 fixes

class Fix01To02WebhookAuthTests(IndiaPostTestCase):
    def _post(self, **headers):
        return self.client.post(
            reverse("indiapost:webhook"),
            data=json.dumps({"article_number": self.shipment.barcode_number, "event_code": "OUT_FOR_DELIVERY"}),
            content_type="application/json",
            **headers,
        )

    def setUp(self):
        super().setUp()
        self.order = self.make_order()
        self.shipment = book_shipment(self.order)

    def test_fails_closed_when_no_token_is_configured(self):
        with override_settings(INDIA_POST_WEBHOOK_TOKEN=""):
            self.assertEqual(self._post().status_code, 503)
            self.assertEqual(self._post(HTTP_X_INDIAPOST_TOKEN="anything").status_code, 503)
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.CONFIRMED)

    def test_rejects_missing_or_wrong_token(self):
        self.assertEqual(self._post().status_code, 401)
        self.assertEqual(self._post(HTTP_X_INDIAPOST_TOKEN="wrong").status_code, 401)

    def test_token_in_query_string_is_not_accepted(self):
        response = self.client.post(
            reverse("indiapost:webhook") + "?token=hook-secret",
            data=json.dumps({"article_number": self.shipment.barcode_number, "event_code": "DELIVERED"}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 401)

    def test_header_and_bearer_tokens_update_the_order(self):
        self.assertEqual(self._post(HTTP_X_INDIAPOST_TOKEN="hook-secret").status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.OUT_FOR_DELIVERY)
        response = self.client.post(
            reverse("indiapost:webhook"),
            data=json.dumps({"article_number": self.shipment.barcode_number, "event_code": "DELIVERED"}),
            content_type="application/json",
            HTTP_AUTHORIZATION="Bearer hook-secret",
        )
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.DELIVERED)

    def test_duplicate_and_out_of_order_events_are_ignored(self):
        self._post(HTTP_X_INDIAPOST_TOKEN="hook-secret")
        body = json.loads(self._post(HTTP_X_INDIAPOST_TOKEN="hook-secret").content)
        self.assertEqual(body["processed_events"], 0)  # duplicate
        late = {"article_number": self.shipment.barcode_number, "event_code": "ITEM_DISPATCHED"}
        self.client.post(reverse("indiapost:webhook"), json.dumps(late), content_type="application/json",
                         HTTP_X_INDIAPOST_TOKEN="hook-secret")
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.OUT_FOR_DELIVERY)  # no regression

    def test_return_scan_cannot_cancel_a_delivered_order(self):
        transition_order_status(order=self.order, new_status=OrderStatus.DELIVERED, force=True)
        payload = {"article_number": self.shipment.barcode_number, "event_code": "RTO"}
        self.client.post(reverse("indiapost:webhook"), json.dumps(payload), content_type="application/json",
                         HTTP_X_INDIAPOST_TOKEN="hook-secret")
        self.order.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.DELIVERED)


class Fix02MobileAndPincodeValidationTests(IndiaPostTestCase):
    def test_normalize_indian_mobile(self):
        valid = ["9876543210", "9123456789", "+919123456789", "919123456789", "09123456789", "+91 98765 43210", "98765-43210"]
        for raw in valid:
            self.assertEqual(len(normalize_indian_mobile(raw)), 10, raw)
        self.assertEqual(normalize_indian_mobile("+919123456789"), "9123456789")
        for raw in ["1234567890", "5876543210", "+441234567890", "98765", "98765432101", "", None, "abcdefghij"]:
            self.assertEqual(normalize_indian_mobile(raw), "", raw)

    def test_normalize_pincode(self):
        self.assertEqual(normalize_pincode("682001"), "682001")
        for raw in ["12345", "1234567", "012345", "68200a", "", None]:
            self.assertEqual(normalize_pincode(raw), "", raw)

    def _checkout(self, **overrides):
        self.client.post(reverse("cart:add"), {"product_id": self.product.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
        self.client.get(reverse("checkout:checkout"))
        data = {
            "gateway_key": "cod", "idempotency_key": "k", "guest_name": "Ravi K", "guest_email": "r@example.com",
            "guest_phone": "9876543210", "guest_address_line1": "12 MG Road", "guest_city": "Kochi",
            "guest_state": "Kerala", "guest_pincode": "682001", **overrides,
        }
        return self.client.post(reverse("checkout:place-order"), data, HTTP_HX_REQUEST="true")

    def test_checkout_accepts_mobiles_starting_with_91(self):
        for phone in ("9123456789", "+91 91234 56789"):
            self.assertNotIn("Indian mobile", self._checkout(guest_phone=phone).content.decode(), phone)

    def test_checkout_rejects_invalid_mobile_and_pincode(self):
        self.assertIn("Indian mobile", self._checkout(guest_phone="1234567890").content.decode())
        self.assertIn("6-digit PIN", self._checkout(guest_pincode="12345").content.decode())

    def test_booking_uses_the_same_mobile_rules(self):
        order = self.make_order(address={
            "name": "R", "phone": "+91 91234 56789", "line1": "a", "city": "b", "state": "c", "pincode": "682001"})
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_mobile_no"], "9123456789")


class Fix03RetryTests(IndiaPostTestCase):
    def test_transient_failure_schedules_a_retry_with_backoff(self):
        order = self.make_order()
        self.net.book_queue.append((503, {"message": "down"}))
        with patch.object(ip_tasks.create_indiapost_shipment_for_order, "retry", side_effect=Retry()) as retry:
            with self.assertRaises(Retry):
                ip_tasks.create_indiapost_shipment_for_order.run(order.pk)
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)

    def test_permanent_failure_is_recorded_and_not_retried(self):
        order = self.make_order(address={"name": "R", "phone": "9876543210", "line1": "a", "city": "b", "pincode": "123"})
        with patch.object(ip_tasks.create_indiapost_shipment_for_order, "retry") as retry:
            self.assertFalse(ip_tasks.create_indiapost_shipment_for_order.run(order.pk))
        retry.assert_not_called()
        self.assertEqual(IndiaPostShipment.objects.get(order=order).tracking_status, ShipmentStatus.FAILED)

    def test_confirmed_order_routes_to_the_retrying_task(self):
        from delhivery.tasks import create_shipment_for_order

        order = self.make_order()
        order.delivery_partner = "indiapost"
        order.save(update_fields=["delivery_partner"])
        with patch("indiapost.tasks.create_indiapost_shipment_for_order.delay") as delay:
            self.assertTrue(create_shipment_for_order(order_id=order.pk))
        delay.assert_called_once_with(order_id=order.pk)


class Fix04AdminActionTests(IndiaPostTestCase):
    def _run_action(self, queryset):
        request = RequestFactory().post("/admin/orders/order/")
        request.user = User.objects.create_superuser("adm", "a@example.com", "pw12345678")
        request.session = {}
        request._messages = FallbackStorage(request)
        OrderAdmin(Order, admin.site).book_with_india_post(request, queryset)
        return [str(m) for m in request._messages]

    def test_already_booked_orders_are_not_resubmitted(self):
        order = self.make_order()
        book_shipment(order)
        messages = self._run_action(Order.objects.filter(pk=order.pk))
        self.assertEqual(len(self.net.booking_calls()), 1)
        self.assertIn("1 already booked", messages[-1])

    def test_failures_are_reported_with_the_reason(self):
        order = self.make_order(status=OrderStatus.CANCELLED)
        messages = self._run_action(Order.objects.filter(pk=order.pk))
        self.assertTrue(any("cancelled" in m.lower() for m in messages))
        self.assertIn("1 failed", messages[-1])


class Fix05ManualBarcodeTests(IndiaPostTestCase):
    def test_manual_barcode_is_never_auto_booked(self):
        order = self.make_order()
        admin_user = User.objects.create_superuser("adm", "a@example.com", "pw12345678")
        self.client.force_login(admin_user)
        order.delivery_partner = "indiapost"
        order.save(update_fields=["delivery_partner"])
        self.client.post(reverse("dashboard:order-tracking-update", kwargs={"pk": order.pk}), {"waybill_number": "EE123456789IN"})
        shipment = IndiaPostShipment.objects.get(order=order)
        self.assertEqual((shipment.barcode_number, shipment.tracking_status), ("EE123456789IN", ShipmentStatus.MANUAL))
        book_shipment(order)
        self.assertEqual(self.net.booking_calls(), [])

    def test_clearing_a_manual_barcode_makes_the_order_bookable_again(self):
        order = self.make_order()
        IndiaPostShipment.objects.create(order=order, barcode_number="EE123456789IN", tracking_status=ShipmentStatus.MANUAL)
        self.client.force_login(User.objects.create_superuser("adm", "a@example.com", "pw12345678"))
        order.delivery_partner = "indiapost"
        order.save(update_fields=["delivery_partner"])
        self.client.post(reverse("dashboard:order-tracking-update", kwargs={"pk": order.pk}), {"waybill_number": ""})
        book_shipment(order)
        self.assertEqual(len(self.net.booking_calls()), 1)

    def test_duplicate_manual_barcode_is_a_friendly_error(self):
        first, second = self.make_order(), self.make_order()
        IndiaPostShipment.objects.create(order=first, barcode_number="EE123456789IN", tracking_status=ShipmentStatus.MANUAL)
        self.client.force_login(User.objects.create_superuser("adm", "a@example.com", "pw12345678"))
        for order in (second,):
            order.delivery_partner = "indiapost"
            order.save(update_fields=["delivery_partner"])
        response = self.client.post(reverse("dashboard:order-tracking-update", kwargs={"pk": second.pk}), {"waybill_number": "EE123456789IN"})
        self.assertEqual(response.status_code, 302)


class Fix06MissingNameTests(IndiaPostTestCase):
    def test_missing_snapshot_name_falls_back_to_the_account_name(self):
        order = self.make_order(address={"phone": "9876543210", "line1": "a", "city": "b", "state": "c", "pincode": "682001"})
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_name"], "Asha Nair")

    def test_guest_order_without_any_name_uses_a_generic_one(self):
        order = self.make_order(address={"phone": "9876543210", "line1": "a", "city": "b", "state": "c", "pincode": "682001"})
        order.customer_profile = None
        order.save(update_fields=["customer_profile"])
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_name"], "Customer")


class Fix07BarcodeReservationTests(IndiaPostTestCase):
    def test_failed_attempts_reuse_one_barcode(self):
        order = self.make_order()
        for _ in range(2):
            self.net.book_queue.append((503, {"message": "down"}))
            with self.assertRaises(IndiaPostBookingError):
                book_shipment(order)
        shipment = book_shipment(order)
        sent = {c["json"]["articles"][0]["barcode_no"] for c in self.net.booking_calls()}
        self.assertEqual(sent, {shipment.barcode_number})
        self.assertEqual(self.pool_used(), 1)

    def test_validation_failures_do_not_consume_barcodes(self):
        order = self.make_order(address={"name": "R", "phone": "9876543210", "line1": "a", "city": "b", "pincode": "123"})
        for _ in range(3):
            with self.assertRaises(IndiaPostBookingError):
                book_shipment(order)
        self.assertEqual(self.pool_used(), 0)


class Fix08BarcodePoolAndBaseUrlTests(IndiaPostTestCase):
    def test_no_pool_means_a_clear_error_never_an_implicit_test_pool(self):
        IndiaPostBarcodePool.objects.all().delete()
        order = self.make_order()
        with self.assertRaises(IndiaPostBookingError) as ctx:
            book_shipment(order)
        self.assertIn("No active India Post barcode pool", str(ctx.exception))
        self.assertEqual(IndiaPostBarcodePool.objects.count(), 0)
        self.assertEqual(self.net.booking_calls(), [])
        self.assertIn("No active", IndiaPostShipment.objects.get(order=order).error_message)

    def test_exhausted_pool_is_recorded_on_the_shipment(self):
        IndiaPostBarcodePool.objects.update(current_number=21433011)
        order = self.make_order()
        with self.assertRaises(IndiaPostBookingError):
            book_shipment(order)
        self.assertIn("exhausted", IndiaPostShipment.objects.get(order=order).error_message)
        with self.assertRaises(BarcodePoolError):
            IndiaPostBarcodePool.allocate_next_barcode()

    def test_seed_command_creates_the_uat_pool_explicitly(self):
        IndiaPostBarcodePool.objects.all().delete()
        out = StringIO()
        call_command("seed_indiapost_pool", "--uat", stdout=out)
        pool = IndiaPostBarcodePool.objects.get()
        self.assertEqual((pool.prefix, pool.current_number, pool.end_number), ("ET", 21433001, 21434000))

    @override_settings(INDIA_POST_BASE_URL="")
    def test_blank_base_url_fails_loudly_instead_of_defaulting_to_test(self):
        with self.assertRaises(IndiaPostConfigError):
            IndiaPostClient().get_access_token()
        self.assertIn("INDIA_POST_BASE_URL is not set", config_problems())
        self.assertEqual(self.net.calls, [])


class Fix09ConfigValidationTests(IndiaPostTestCase):
    def test_each_missing_setting_is_reported_before_any_request(self):
        cases = {
            "INDIA_POST_CUSTOMER_ID": "CUSTOMER_ID",
            "INDIA_POST_CONTRACT_ID_SP": "CONTRACT_ID_SP",
            "INDIA_POST_ORIGIN_OFFICE_ID": "ORIGIN_OFFICE_ID",
            "INDIA_POST_SENDER_MOBILE": "SENDER_MOBILE",
            "INDIA_POST_ORIGIN_PINCODE": "ORIGIN_PINCODE",
            "INDIA_POST_USERNAME": "USERNAME",
        }
        for setting_name, fragment in cases.items():
            blank = 0 if setting_name.endswith("OFFICE_ID") else ""
            with override_settings(**{setting_name: blank}):
                order = self.make_order()
                with self.assertRaises(IndiaPostBookingError) as ctx:
                    book_shipment(order)
                self.assertIn(fragment, str(ctx.exception))
        self.assertEqual(self.net.calls, [])
        self.assertEqual(self.pool_used(), 0)

    def test_complete_configuration_has_no_problems(self):
        self.assertEqual(config_problems(), [])

    def test_label_with_missing_config_does_not_crash(self):
        shipment = book_shipment(self.make_order())
        with override_settings(INDIA_POST_CUSTOMER_ID=""):
            self.assertFalse(generate_and_save_shipping_label(shipment))
        shipment.refresh_from_db()
        self.assertIn("label generation failed", shipment.error_message)


class Fix10TrackingSyncTests(IndiaPostTestCase):
    def _booked(self, status=OrderStatus.CONFIRMED):
        order = self.make_order(status=status)
        shipment = book_shipment(order)
        return order, shipment

    def test_closed_orders_are_not_polled_and_open_ones_are(self):
        done_order, _ = self._booked()
        done_order.order_status = OrderStatus.DELIVERED
        done_order.save(update_fields=["order_status"])
        open_order, open_shipment = self._booked()
        ip_tasks.sync_indiapost_tracking_task()
        polled = self.net.tracking_calls()[0]["json"]["bulk"]
        self.assertEqual(polled, [open_shipment.barcode_number])

    def test_a_delivery_event_closes_the_shipment_for_future_syncs(self):
        order, shipment = self._booked()
        self.net.tracking_data = [FakeIndiaPost.track_record(shipment.barcode_number, "Item Delivered", delivered=True)]
        self.assertEqual(ip_tasks.sync_indiapost_tracking_task(), 1)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.DELIVERED)
        self.net.calls.clear()
        ip_tasks.sync_indiapost_tracking_task()
        self.assertEqual(self.net.tracking_calls(), [])

    def test_least_recently_synced_shipments_go_first_and_batches_are_split(self):
        shipments = [self._booked()[1] for _ in range(3)]
        IndiaPostShipment.objects.filter(pk=shipments[0].pk).update(last_status_at=timezone.now())
        with patch.object(ip_tasks, "TRACKING_BATCH_SIZE", 2):
            ip_tasks.sync_indiapost_tracking_task()
        batches = [c["json"]["bulk"] for c in self.net.tracking_calls()]
        self.assertEqual([len(b) for b in batches], [2, 1])
        self.assertEqual(batches[-1], [shipments[0].barcode_number])  # synced most recently -> last

    def test_unbooked_shipments_are_never_polled(self):
        order = self.make_order()
        IndiaPostShipment.objects.create(order=order, barcode_number="ET000000000IN", tracking_status=ShipmentStatus.FAILED)
        ip_tasks.sync_indiapost_tracking_task()
        self.assertEqual(self.net.tracking_calls(), [])


class Fix13SavedAddressPincodeTests(IndiaPostTestCase):
    def test_address_model_and_forms_reject_bad_pincodes(self):
        address = Address(customer_profile=self.profile, label="Home", line1="a", city="Kochi", pincode="123")
        with self.assertRaises(ValidationError):
            address.full_clean()
        self.assertFalse(AddressForm({"label": "Home", "line1": "a", "city": "Kochi", "pincode": "123"}).is_valid())
        self.assertFalse(
            CustomerProfileEditForm({"name": "A B", "pincode": "1234567", "phone": "9876543210"}).is_valid()
        )
        self.assertFalse(CustomerProfileEditForm({"name": "A B", "phone": "12345"}).is_valid())
        form = OrderAddressForm({"line1": "a", "city": "b", "pincode": "99", "phone": "9876543210"})
        self.assertFalse(form.is_valid())
        self.assertIn("pincode", form.errors)

    def test_valid_values_still_pass(self):
        self.assertTrue(OrderAddressForm({"line1": "a", "city": "b", "pincode": "682001", "phone": "9876543210"}).is_valid())

    def test_checkout_rejects_a_saved_address_with_an_invalid_pin(self):
        bad = Address.objects.create(customer_profile=self.profile, label="Old", line1="a", city="Kochi", state="Kerala", pincode="123")
        self.client.force_login(self.user)
        self.client.post(reverse("cart:add"), {"product_id": self.product.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
        self.client.get(reverse("checkout:checkout"))
        response = self.client.post(
            reverse("checkout:place-order"),
            {"gateway_key": "cod", "idempotency_key": "k", "address_id": bad.pk, "guest_name": "Asha Nair",
             "guest_email": "buyer@example.com", "guest_phone": "9876543210"},
            HTTP_HX_REQUEST="true",
        )
        self.assertIn("PIN code on your saved address", response.content.decode())
        self.assertFalse(Order.objects.filter(delivery_address_snapshot__line1="a", customer_profile=self.profile, order_status=OrderStatus.CHECKOUT_PENDING).exists())


class SavedAddressPostOfficeTests(IndiaPostTestCase):
    """With India Post as the courier, saved addresses need a post office just like guest checkout."""

    def _set_carrier(self, carrier):
        site = SiteSettings.objects.first() or SiteSettings.objects.create()
        site.shipping_carrier = carrier
        site.save()

    def _checkout_with(self, address):
        self.client.force_login(self.user)
        self.client.post(reverse("cart:add"), {"product_id": self.product.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
        page = self.client.get(reverse("checkout:checkout")).content.decode()
        response = self.client.post(
            reverse("checkout:place-order"),
            {"gateway_key": "cod", "idempotency_key": "k", "address_id": address.pk, "guest_name": "Asha Nair",
             "guest_email": "buyer@example.com", "guest_phone": "9876543210"},
            HTTP_HX_REQUEST="true",
        )
        return page, response.content.decode()

    def _address(self, line2):
        return Address.objects.create(
            customer_profile=self.profile, label="Home", line1="12 MG Road", line2=line2,
            city="Kochi", state="Kerala", pincode="682001",
        )

    def test_checkout_rejects_a_saved_address_without_a_post_office(self):
        self._set_carrier("indiapost")
        page, result = self._checkout_with(self._address(""))
        self.assertIn("Post Office missing", page)
        self.assertIn("Your saved address &quot;Home&quot; needs a Post Office: Post Office is required.", result)

    def test_checkout_rejects_a_saved_address_with_a_too_short_post_office(self):
        self._set_carrier("indiapost")
        _, result = self._checkout_with(self._address("HO"))
        self.assertIn("between 3 and 80 characters", result)

    def test_checkout_accepts_a_saved_address_with_a_post_office(self):
        self._set_carrier("indiapost")
        page, result = self._checkout_with(self._address("Aluva H.O"))
        self.assertNotIn("Post Office missing", page)
        self.assertNotIn("needs a Post Office", result)

    def test_other_carriers_do_not_need_a_post_office_on_saved_addresses(self):
        self._set_carrier("delhivery")
        page, result = self._checkout_with(self._address(""))
        self.assertNotIn("Post Office missing", page)
        self.assertNotIn("needs a Post Office", result)

    def test_profile_form_requires_a_post_office_only_in_india_post_mode(self):
        data = {"name": "A B", "phone": "9876543210", "address_line1": "12 MG Road", "city": "Kochi",
                "state": "Kerala", "pincode": "682001", "address_line2": ""}
        form = CustomerProfileEditForm(data, post_office_mode=True)
        self.assertFalse(form.is_valid())
        self.assertIn("Post Office is required.", form.errors["address_line2"])
        self.assertFalse(CustomerProfileEditForm({**data, "address_line2": "HO"}, post_office_mode=True).is_valid())
        self.assertTrue(CustomerProfileEditForm({**data, "address_line2": "Aluva H.O"}, post_office_mode=True).is_valid())
        self.assertTrue(CustomerProfileEditForm(data, post_office_mode=False).is_valid())
        #no address being saved (line 1 empty) -> nothing to check
        self.assertTrue(CustomerProfileEditForm({"name": "A B", "phone": "9876543210"}, post_office_mode=True).is_valid())

    def test_profile_page_labels_the_field_post_office(self):
        self._set_carrier("indiapost")
        self.client.force_login(self.user)
        html = self.client.get(reverse("accounts:edit-profile")).content.decode()
        self.assertIn("Post Office", html)
        self.assertNotIn("Address Line 2 (Optional)", html)

    def test_address_form_requires_a_post_office_only_in_india_post_mode(self):
        data = {"label": "Home", "line1": "12 MG Road", "city": "Kochi", "state": "Kerala", "pincode": "682001"}
        self._set_carrier("indiapost")
        self.assertIn("line2", AddressForm(data).errors)
        self.assertTrue(AddressForm({**data, "line2": "Aluva H.O"}).is_valid(), AddressForm({**data, "line2": "Aluva H.O"}).errors)
        self._set_carrier("delhivery")
        self.assertTrue(AddressForm(data).is_valid())


class Fix14ConcurrencyTests(IndiaPostTestCase):
    def test_a_booking_in_progress_blocks_a_second_one(self):
        order = self.make_order()
        IndiaPostShipment.objects.create(order=order, tracking_status=ShipmentStatus.BOOKING)
        with self.assertRaises(IndiaPostBookingError) as ctx:
            book_shipment(order)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(self.net.booking_calls(), [])

    def test_a_stale_claim_from_a_crashed_worker_can_be_taken_over(self):
        order = self.make_order()
        shipment = IndiaPostShipment.objects.create(order=order, tracking_status=ShipmentStatus.BOOKING)
        IndiaPostShipment.objects.filter(pk=shipment.pk).update(updated_at=timezone.now() - CLAIM_STALE_AFTER - timedelta(minutes=1))
        self.assertEqual(book_shipment(order).tracking_status, ShipmentStatus.BOOKED)

    def test_a_second_request_arriving_mid_booking_sends_nothing(self):
        order = self.make_order()
        outcomes = []

        def second_caller():
            try:
                book_shipment(order)
                outcomes.append("booked")
            except IndiaPostBookingError as exc:
                outcomes.append(("blocked", exc.retryable))

        self.net.on_book = second_caller
        book_shipment(order)
        self.assertEqual(outcomes, [("blocked", True)])
        self.assertEqual(len(self.net.booking_calls()), 1)

    def test_a_crash_never_leaves_the_claim_dangling(self):
        order = self.make_order()
        with patch("indiapost.services.build_article_payload", side_effect=RuntimeError("boom")):
            with self.assertRaises(IndiaPostBookingError) as ctx:
                book_shipment(order)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(IndiaPostShipment.objects.get(order=order).tracking_status, ShipmentStatus.FAILED)
        self.assertEqual(book_shipment(order).tracking_status, ShipmentStatus.BOOKED)


# ============================================================================ client

class ClientTests(IndiaPostTestCase):
    def test_login_token_is_cached_and_scoped_to_host_and_user(self):
        client = IndiaPostClient()
        client.get_access_token()
        client.get_access_token()
        self.assertEqual(self.net.logins, 1)
        other = IndiaPostClient(base_url="https://api.indiapost.example/prod")
        self.assertNotEqual(client._token_cache_key, other._token_cache_key)

    def test_expired_token_is_refreshed_and_the_call_retried_once(self):
        client = IndiaPostClient()
        cache.set(client._token_cache_key, "stale-token")
        self.assertTrue(client.search_pincode("682001"))
        self.assertEqual(self.net.logins, 1)
        statuses = [c["headers"].get("Authorization") for c in self.net.calls if "pincode" in c["url"]]
        self.assertEqual(statuses, ["Bearer stale-token", "Bearer tok-1"])

    def test_invalid_credentials_raise_an_auth_error(self):
        self.net.login_ok = False
        with self.assertRaises(IndiaPostAuthError):
            IndiaPostClient().get_access_token()
        self.assertIsNone(cache.get(IndiaPostClient()._token_cache_key))

    def test_error_classes(self):
        client = IndiaPostClient()
        self.net.book_queue += [(400, {"errors": ["x"]}), (503, {})]
        with self.assertRaises(IndiaPostAPIError) as api:
            client.book_articles("1", [{}])
        self.assertFalse(api.exception.retryable)
        self.assertEqual(api.exception.body, {"errors": ["x"]})
        with self.assertRaises(IndiaPostTransientError) as transient:
            client.book_articles("1", [{}])
        self.assertTrue(transient.exception.retryable)

    def test_network_error_is_transient(self):
        import requests

        with patch("indiapost.client.requests.request", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(IndiaPostTransientError):
                IndiaPostClient().get_access_token()

    def test_recorder_masks_credentials_and_tokens(self):
        recorded = []
        client = IndiaPostClient(recorder=recorded.append)
        client.search_pincode("682001")
        dumped = json.dumps(recorded)
        self.assertNotIn("uat-pass", dumped)
        self.assertNotIn("tok-1", dumped)
        self.assertIn("***", dumped)
        self.assertEqual([e["status_code"] for e in recorded], [200, 200])


# ============================================================================ UAT runner

class UATRunnerTests(IndiaPostTestCase):
    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.out, True)

    def _run(self, **kwargs):
        from indiapost.uat import UATRunner, write_report

        runner = UATRunner(output_dir=self.out, **kwargs)
        results = runner.run()
        write_report(results, self.out)
        return {r.id: r for r in results}

    def test_all_scenarios_pass_against_a_healthy_server(self):
        results = self._run()
        failed = {k: (v.status, v.actual) for k, v in results.items() if v.status not in ("PASS", "SKIPPED")}
        self.assertEqual(failed, {})
        self.assertEqual(len(results), 18)

    def test_evidence_and_report_are_written_and_secrets_are_masked(self):
        self._run()
        for scenario_id in ("UAT-01", "UAT-09", "UAT-13"):
            self.assertTrue((self.out / f"{scenario_id}.json").exists(), scenario_id)
        report = (self.out / "UAT_TEST_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("ALL EXECUTED SCENARIOS PASSED", report)
        self.assertIn("UAT-12", report)
        self.assertTrue(list(self.out.glob("label_*.pdf")))
        everything = report + "".join(p.read_text(encoding="utf-8") for p in self.out.glob("*.json"))
        self.assertNotIn("uat-pass", everything)
        self.assertNotIn("tok-1", everything)
        login = json.loads((self.out / "UAT-01.json").read_text())["exchanges"][0]
        self.assertEqual(login["request_body"]["password"], "***")

    def test_token_refresh_scenario_records_the_401_then_relogin_sequence(self):
        result = self._run()["UAT-03"]
        statuses = [e["status_code"] for e in result.exchanges]
        self.assertEqual(statuses[0], 401)
        self.assertIn("/v1/access/login", result.exchanges[1]["url"])

    def test_failure_scenarios_send_no_booking_request_for_local_errors(self):
        results = self._run()
        for scenario_id in ("UAT-13", "UAT-14"):
            self.assertFalse([e for e in results[scenario_id].exchanges if "/process-articles/" in e.get("url", "")])

    def test_uat_orders_are_cancelled_afterwards_so_they_never_count_as_sales(self):
        self._run()
        orders = Order.objects.filter(order_number__startswith="UAT-")
        self.assertTrue(orders.exists())
        self.assertFalse(orders.exclude(order_status=OrderStatus.CANCELLED).exists())

    def test_a_broken_server_is_reported_as_failures_not_hidden(self):
        self.net.login_ok = False
        results = self._run()
        self.assertEqual(results["UAT-01"].status, "ERROR")
        self.assertEqual(results["UAT-02"].status, "PASS")  # invalid credentials are *expected* to fail
        self.assertEqual(results["UAT-10"].status, "ERROR")
        self.assertEqual(results["UAT-11"].status, "SKIPPED")
        report = (self.out / "UAT_TEST_REPORT.md").read_text(encoding="utf-8")
        self.assertIn("NOT ALL SCENARIOS PASSED", report)

    def test_command_refuses_a_non_uat_host_without_the_flag(self):
        from django.core.management.base import CommandError

        with override_settings(INDIA_POST_BASE_URL="https://api.indiapost.example/prod"):
            with self.assertRaises(CommandError) as ctx:
                call_command("indiapost_uat", output_dir=str(self.out))
        self.assertIn("Refusing to run", str(ctx.exception))

    def test_command_runs_end_to_end_and_exits_cleanly(self):
        out = StringIO()
        reachable = ConnectionCheck(True, "ready", "TLS connection to test.cept.gov.in:443 succeeded.")
        with patch("indiapost.management.commands.indiapost_uat.check_connection", return_value=reachable):
            call_command("indiapost_uat", output_dir=str(self.out), stdout=out)
        self.assertIn("UAT-18", out.getvalue())
        self.assertTrue((self.out / "UAT_TEST_REPORT.md").exists())


class PostOfficeCheckoutTests(IndiaPostTestCase):
    """With India Post as the courier, checkout asks for the Post Office instead of address line 2."""

    def _set_carrier(self, carrier):
        site = SiteSettings.objects.first() or SiteSettings.objects.create()
        site.shipping_carrier = carrier
        site.save()

    def _page(self):
        self.client.post(reverse("cart:add"), {"product_id": self.product.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
        return self.client.get(reverse("checkout:checkout")).content.decode()

    def _place(self, **overrides):
        data = {
            "gateway_key": "cod", "idempotency_key": "k", "guest_name": "Ravi K", "guest_email": "r@example.com",
            "guest_phone": "9876543210", "guest_address_line1": "12 MG Road", "guest_address_line2": "",
            "guest_city": "Kochi", "guest_state": "Kerala", "guest_pincode": "682001", **overrides,
        }
        return self.client.post(reverse("checkout:place-order"), data, HTTP_HX_REQUEST="true").content.decode()

    def test_india_post_mode_shows_a_required_post_office_field(self):
        self._set_carrier("indiapost")
        html = self._page()
        self.assertIn("Post Office", html)
        self.assertNotIn("Address Line 2 (Optional)", html)
        self.assertIn("var postOfficeMode = true;", html)

    def test_other_carriers_keep_the_optional_address_line_2(self):
        self._set_carrier("delhivery")
        html = self._page()
        self.assertIn("Address Line 2 (Optional)", html)
        self.assertNotIn("Your nearest / delivery post office", html)
        self.assertIn("var postOfficeMode = false;", html)

    def test_post_office_is_required_server_side_only_in_india_post_mode(self):
        self._set_carrier("indiapost")
        self._page()
        self.assertIn("Post Office is required.", self._place())
        self.assertIn("valid post office name", self._place(guest_address_line2="12345"))
        self.assertNotIn("Post Office is required.", self._place(guest_address_line2="Aluva H.O"))
        self._set_carrier("delhivery")
        self.assertNotIn("Post Office is required.", self._place())

    def test_post_office_travels_to_india_post_as_address_line_2(self):
        order = self.make_order(address={
            "name": "R", "phone": "9876543210", "line1": "12 MG Road", "line2": "Aluva H.O",
            "city": "Kochi", "state": "Kerala", "pincode": "682001"})
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_add_line_2"], "Aluva H.O")

    def test_post_office_must_be_3_to_80_characters(self):
        """India Post rejects an address line 2 outside 3-80 characters, so checkout does too."""
        self._set_carrier("indiapost")
        self._page()
        self.assertIn("between 3 and 80 characters", self._place(guest_address_line2="HO"))
        self.assertIn("between 3 and 80 characters", self._place(guest_address_line2="A" * 81))
        self.assertNotIn("between 3 and 80 characters", self._place(guest_address_line2="Aluva H.O"))
        self.assertIn('maxlength="80"', self._page())

    def test_post_office_error_is_cleared_before_each_validation(self):
        """A fixed Post Office must not keep showing the error from the previous attempt."""
        self._set_carrier("indiapost")
        html = self._page()
        error_ids = html.split("var errorIds = [", 1)[1].split("];", 1)[0]
        self.assertIn("'error-guest_address_line2'", error_ids)

    def test_short_line_2_on_an_existing_address_is_made_india_post_safe(self):
        """Addresses saved before the 3-character rule must not make the booking fail."""
        order = self.make_order(address={
            "name": "R", "phone": "9876543210", "line1": "12 MG Road", "line2": "HO",
            "city": "Kochi", "state": "Kerala", "pincode": "682001"})
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_add_line_2"], "HO.")

    def test_blank_line_2_stays_blank(self):
        order = self.make_order(address={
            "name": "R", "phone": "9876543210", "line1": "12 MG Road", "line2": "",
            "city": "Kochi", "state": "Kerala", "pincode": "682001"})
        book_shipment(order)
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["receiver_add_line_2"], "")


# ============================================================================ API specification

class ParcelClassificationTests(IndiaPostTestCase):
    """Article type, shape and limits follow India Post's Speed Post specification."""

    def _lines(self, weight_kg, qty=1, length=None, width=None, height=None):
        self.product.weight = Decimal(str(weight_kg))
        self.product.length, self.product.width, self.product.height = length, width, height
        return [(self.product, qty)]

    def test_light_flat_article_is_a_document(self):
        from indiapost.services import plan_parcel

        parcel = plan_parcel(self._lines("0.4", length=30, width=20, height=2))
        self.assertEqual((parcel.article_type, parcel.shape, parcel.weight_grams), ("SP_INLAND_DOC", "DOC", 400))

    def test_light_but_thick_article_is_a_parcel(self):
        from indiapost.services import plan_parcel

        parcel = plan_parcel(self._lines("0.4", length=25, width=20, height=3))
        self.assertEqual((parcel.article_type, parcel.shape), ("SP_INLAND_PARCEL", "NROL"))

    def test_heavy_article_is_a_parcel_with_parcel_minimums(self):
        from indiapost.services import plan_parcel

        parcel = plan_parcel(self._lines("1.2"))
        self.assertEqual((parcel.article_type, parcel.length, parcel.width), ("SP_INLAND_PARCEL", 14, 9))

    def test_quantity_multiplies_weight_and_missing_weight_defaults_to_500g(self):
        from indiapost.services import plan_parcel

        self.assertEqual(plan_parcel(self._lines("0.4", qty=3)).weight_grams, 1200)
        self.assertEqual(plan_parcel(self._lines(0)).weight_grams, 500)

    def test_over_limits_are_refused(self):
        from indiapost.services import plan_parcel

        with self.assertRaises(IndiaPostBookingError):
            plan_parcel(self._lines("36"))
        with self.assertRaises(IndiaPostBookingError):
            plan_parcel(self._lines("1", length=151, width=10, height=10))

    def test_short_names_are_padded_to_the_three_character_minimum(self):
        order = self.make_order(address={"name": "Li", "phone": "9876543210", "line1": "ab", "city": "Pu", "state": "Kerala", "pincode": "682001"})
        book_shipment(order)
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        for field in ("receiver_name", "receiver_company", "receiver_add_line_1", "receiver_city"):
            self.assertGreaterEqual(len(article[field]), 3, field)
        self.assertEqual(article["receiver_name"], "Li.")


class LabelPayloadTests(IndiaPostTestCase):
    def test_label_always_uses_contract_payment_and_payment_completed(self):
        for cod in (False, True):
            self.net.calls.clear()
            book_shipment(self.make_order(cod=cod))
            label = next(c for c in self.net.calls if c["url"].endswith("/v1/label/create/domestic"))["json"][0]
            self.assertEqual((label["payment_mode"], label["payment_status"]), ("CO", "PC"), f"cod={cod}")
            self.assertEqual(label["physical_weight"], 400.0)


class TrackingResponseTests(IndiaPostTestCase):
    def test_parses_the_documented_response_shape(self):
        from indiapost.services import extract_tracking_items

        data = {"success": True, "data": [
            FakeIndiaPost.track_record("RK1", "Taken out for delivery"),
            FakeIndiaPost.track_record("RK2", "Item Kept on Hold", remarks="Intimation Delivered"),
            FakeIndiaPost.track_record("RK3", "Item Delivered", delivered=True),
            FakeIndiaPost.track_record("RK4", "Item Dispatched", rts=True),
        ]}
        items = {i["article_number"]: i for i in extract_tracking_items(data)}
        self.assertEqual(items["RK1"]["event_code"], "TAKEN_OUT_FOR_DELIVERY")
        self.assertEqual(items["RK2"]["event_description"], "Intimation Delivered")
        self.assertEqual(items["RK3"]["event_code"], "DELIVERED")
        self.assertTrue(items["RK4"]["rts"])

    def test_latest_event_is_chosen_by_date_not_position(self):
        from indiapost.services import extract_tracking_items

        record = FakeIndiaPost.track_record("RK1", "Item Received", date="2026-09-06T01:00:00Z")
        record["tracking_details"].append({"date": "2026-09-08T09:00:00Z", "event": "Item Delivered", "remarks": "", "rts": False})
        self.assertEqual(extract_tracking_items({"data": [record]})[0]["event_code"], "ITEM_DELIVERED")

    def test_sync_moves_orders_from_real_event_names(self):
        order = self.make_order()
        shipment = book_shipment(order)
        self.net.tracking_data = [FakeIndiaPost.track_record(shipment.barcode_number, "Taken out for delivery")]
        ip_tasks.sync_indiapost_tracking_task()
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.OUT_FOR_DELIVERY)

    def test_rts_flag_returns_the_order_to_the_store(self):
        order = self.make_order()
        shipment = book_shipment(order)
        self.net.tracking_data = [FakeIndiaPost.track_record(shipment.barcode_number, "Item Dispatched", rts=True)]
        ip_tasks.sync_indiapost_tracking_task()
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.CANCELLED)


class ReversePickupTests(IndiaPostTestCase):
    """A return is a PICKUP booking: the customer is the sender, the store the receiver."""

    def _book(self, **overrides):
        from datetime import date

        from indiapost.services import book_reverse_pickup, order_lines, resolve_recipient

        order = self.make_order()
        kwargs = dict(
            barcode=IndiaPostBarcodePool.allocate_next_barcode(), reference="RET-1",
            pickup_from=resolve_recipient(order), lines=order_lines(order), day=date(2026, 12, 15), slot="10:00-13:00",
        )
        kwargs.update(overrides)
        return order, book_reverse_pickup(**kwargs)

    def test_books_a_pickup_from_the_customer_to_the_store(self):
        order, booking = self._book()
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual(article["pickup_or_dropoff"], "PICKUP")
        self.assertEqual(article["pickup_address_flag"], "TRUE")
        self.assertEqual(article["pickup_dropoff_office_id"], 21000001)  # from the PIN code search
        self.assertEqual(article["sender_pincode"], "682001")             # customer
        self.assertEqual(article["receiver_pincode"], "683105")           # store
        self.assertEqual(article["sender_mobile_no"], "9876543210")
        self.assertEqual(article["receiver_mobile_no"], "9876501234")
        self.assertEqual(article["bulk_reference"], "RET-1")
        self.assertEqual(booking.barcode, article["barcode_no"])
        self.assertEqual(booking.tariff, Decimal("55.5"))

    def test_short_customer_line_2_is_made_india_post_safe_on_returns(self):
        from dataclasses import replace

        from indiapost.services import resolve_recipient

        customer = replace(resolve_recipient(self.make_order()), line2="HO")
        self._book(pickup_from=customer)
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual(article["sender_add_line_2"], "HO.")
        self.assertEqual(article["pickup_address_line2"], "HO.")

    def test_pickup_fields_follow_the_documented_formats(self):
        self._book()
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual(article["pickup_schedule_slot"], "10:00-13:00")
        self.assertEqual(article["pickup_schedule_date"], "12/15/2026 10:00:00 AM")
        for field in ("pickup_addressee_name", "pickup_company_name", "pickup_address_line1", "pickup_city", "pickup_pincode"):
            self.assertTrue(article[field], field)
        self.assertEqual(article["pickup_mobile_no"], "9876543210")
        self.assertEqual(article["drop_off_pincode"], "")

    def test_afternoon_slot_uses_1_pm(self):
        self._book(slot="13:00-16:00")
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["pickup_schedule_date"], "12/15/2026 01:00:00 PM")

    def test_invalid_slot_is_refused_before_any_request(self):
        with self.assertRaises(IndiaPostBookingError):
            self._book(slot="09:00-10:00")
        self.assertEqual(self.net.calls, [])

    def test_no_office_for_the_pickup_pin_is_a_clear_error(self):
        from datetime import date

        from indiapost.services import Party, book_reverse_pickup

        no_office = type("NoOffice", (), {"get_eligible_office_id": lambda self, pin: None})()
        with self.assertRaises(IndiaPostBookingError) as ctx:
            book_reverse_pickup(
                barcode="ET000000000IN", reference="R", pickup_from=Party("R", "a", "", "b", "c", "682001", "9876543210"),
                lines=[], day=date(2026, 12, 15), slot="10:00-13:00", client=no_office,
            )
        self.assertIn("no delivery office", str(ctx.exception))

    def test_rejection_and_outage_are_classified(self):
        self.net.book_queue += [(400, {"success": False, "errors": ["bad pickup"]}), (503, {})]
        with self.assertRaises(IndiaPostBookingError) as rejected:
            self._book()
        self.assertFalse(rejected.exception.retryable)
        with self.assertRaises(IndiaPostBookingError) as outage:
            self._book()
        self.assertTrue(outage.exception.retryable)



# ============================================================================ connectivity diagnosis

class ConnectionCheckTests(IndiaPostTestCase):
    URL = "https://test.cept.gov.in/beextcustomer"

    def test_reachable_server(self):
        tls = MagicMock()
        tls.__enter__ = lambda s: s
        tls.__exit__ = lambda *a: False
        raw = MagicMock()
        raw.__enter__ = lambda s: s
        raw.__exit__ = lambda *a: False
        with patch("indiapost.uat.socket.gethostbyname", return_value="1.2.3.4"), \
             patch("indiapost.uat.socket.create_connection", return_value=raw), \
             patch("indiapost.uat.ssl.create_default_context") as ctx:
            ctx.return_value.wrap_socket.return_value = tls
            result = check_connection(self.URL)
        self.assertTrue(result.ok)
        self.assertEqual(result.stage, "ready")

    def test_dns_failure(self):
        with patch("indiapost.uat.socket.gethostbyname", side_effect=OSError("no such host")):
            result = check_connection(self.URL)
        self.assertEqual((result.ok, result.stage), (False, "dns"))

    def test_blocked_port(self):
        with patch("indiapost.uat.socket.gethostbyname", return_value="1.2.3.4"), \
             patch("indiapost.uat.socket.create_connection", side_effect=OSError("timed out")):
            result = check_connection(self.URL)
        self.assertEqual((result.ok, result.stage), (False, "tcp"))
        self.assertIn("firewall", result.hint)

    def test_tls_reset_is_diagnosed_as_an_ip_allowlist_with_the_public_ip(self):
        raw = MagicMock()
        raw.__enter__ = lambda s: s
        raw.__exit__ = lambda *a: False
        with patch("indiapost.uat.socket.gethostbyname", return_value="1.2.3.4"), \
             patch("indiapost.uat.socket.create_connection", return_value=raw), \
             patch("indiapost.uat.ssl.create_default_context") as ctx, \
             patch("indiapost.uat._public_ip", return_value="203.0.113.7"):
            ctx.return_value.wrap_socket.side_effect = ConnectionResetError("connection reset")
            result = check_connection(self.URL)
        self.assertEqual((result.ok, result.stage), (False, "tls"))
        self.assertIn("whitelist", result.hint)
        self.assertIn("203.0.113.7", result.hint)
        self.assertIn("integrations.cept@indiapost.gov.in", result.hint)

    def test_empty_url(self):
        self.assertFalse(check_connection("").ok)

    def test_command_stops_with_the_diagnosis_instead_of_running_every_scenario(self):
        from django.core.management.base import CommandError

        blocked = ConnectionCheck(False, "tls", "dropped the TLS handshake", "Ask India Post to whitelist 203.0.113.7")
        out_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out_dir, True)
        with patch("indiapost.management.commands.indiapost_uat.check_connection", return_value=blocked):
            with self.assertRaises(CommandError) as ctx:
                call_command("indiapost_uat", output_dir=str(out_dir))
        self.assertIn("203.0.113.7", str(ctx.exception))
        self.assertEqual(list(out_dir.iterdir()), [])  # nothing was run or written

    def test_the_check_can_be_skipped(self):
        out_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, out_dir, True)
        with patch("indiapost.management.commands.indiapost_uat.check_connection") as check:
            call_command("indiapost_uat", output_dir=str(out_dir), skip_connection_check=True, stdout=StringIO())
        check.assert_not_called()

