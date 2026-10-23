"""India Post and Delhivery must work independently of each other.

Each carrier has to book, track and update orders with the *other* carrier completely
unconfigured, a courier switch must only affect orders confirmed afterwards, and a scan from one
courier must never touch the other's shipments.
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product
from core.models import Currency, SiteSettings
from delhivery.models import DelhiveryShipment
from delhivery.tasks import create_shipment_for_order
from delhivery.tests import DELHIVERY_CFG, GOOD, fake_response
from indiapost import tasks as ip_tasks
from indiapost.models import IndiaPostBarcodePool, IndiaPostShipment, ShipmentStatus
from indiapost.services import book_shipment
from indiapost.tests import CFG as INDIA_POST_CFG, FakeIndiaPost
from orders.models import Order, OrderItem, OrderStatus
from orders.services import transition_order_status
from payments.models import PaymentStatus, PaymentTransaction

NO_INDIA_POST = {k: ("" if not isinstance(v, int) else 0) for k, v in INDIA_POST_CFG.items() if k.startswith("INDIA_POST_")}
NO_DELHIVERY = dict(DELHIVERY_API_KEY="", DELHIVERY_WEBHOOK_TOKEN="")


class CarrierTestCase(TestCase):
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
        self.site = SiteSettings.objects.first() or SiteSettings.objects.create()
        category = Category.objects.create(name="Gym", slug="gym")
        self.product = Product.objects.create(
            name="Hoodie", slug="hoodie", sku="H1", category=category, base_price="500", mrp="500",
            purchase_price="300", stock_quantity=20, weight="0.400",
        )
        IndiaPostBarcodePool.objects.create(prefix="ET", current_number=21433001, end_number=21433050, suffix="IN")

    def use_carrier(self, carrier):
        SiteSettings.objects.update(shipping_carrier=carrier)

    def make_order(self, *, status=OrderStatus.CONFIRMED, partner=None):
        CarrierTestCase.seq += 1
        order = Order.objects.create(
            order_number=f"#C{CarrierTestCase.seq}", order_status=status, success_at=timezone.now(),
            subtotal=Decimal("500"), total_amount=Decimal("500"), currency=self.currency, delivery_partner=partner,
            delivery_address_snapshot={"name": "Ravi K", "phone": "9876543210", "line1": "12 MG Road", "line2": "Aluva H.O",
                                       "city": "Kochi", "state": "Kerala", "pincode": "682001"},
        )
        OrderItem.objects.create(order=order, product=self.product, quantity=1, unit_price=Decimal("500"))
        PaymentTransaction.objects.create(order=order, gateway_key="razorpay_upi", amount=order.total_amount,
                                          currency=self.currency, status=PaymentStatus.SUCCESS)
        return order


# ============================================================================ each works alone

@override_settings(**DELHIVERY_CFG, **NO_INDIA_POST)
class DelhiveryAloneTests(CarrierTestCase):
    def test_books_and_tracks_with_india_post_completely_unconfigured(self):
        self.use_carrier("delhivery")
        order = self.make_order()
        with patch("urllib.request.urlopen", return_value=fake_response(GOOD)) as urlopen:
            transition_order_status(order=order, new_status=OrderStatus.CONFIRMED, force=True)  # CONFIRMED -> same: no-op
            order.order_status = OrderStatus.CHECKOUT_PENDING
            order.save()
            transition_order_status(order=order, new_status=OrderStatus.CONFIRMED, force=True)
            self.assertTrue(create_shipment_for_order(order_id=order.pk))
        urlopen.assert_called_once()
        self.assertEqual(self.net.calls, [], "India Post must not be contacted")
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "delhivery")
        self.assertEqual(DelhiveryShipment.objects.get(order=order).waybill_number, "DL1234567890")
        self.assertFalse(IndiaPostShipment.objects.exists())

        response = self.client.post(
            reverse("delhivery:webhook"),
            json.dumps({"waybill": "DL1234567890", "status": "delivered", "status_datetime": "2026-10-01T10:00:00"}),
            content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token",
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.DELIVERED)
        self.assertEqual(self.net.calls, [])

    def test_the_india_post_sync_job_is_harmless_when_india_post_is_unconfigured(self):
        order = self.make_order(partner="delhivery")
        DelhiveryShipment.objects.create(order=order, waybill_number="DL1", tracking_status="In Transit")
        self.assertEqual(ip_tasks.sync_indiapost_tracking_task(), 0)
        self.assertEqual(self.net.calls, [])


@override_settings(**INDIA_POST_CFG, **NO_DELHIVERY)
class IndiaPostAloneTests(CarrierTestCase):
    def test_books_and_tracks_with_delhivery_completely_unconfigured(self):
        self.use_carrier("indiapost")
        order = self.make_order(status=OrderStatus.CHECKOUT_PENDING)
        with patch("urllib.request.urlopen") as urlopen:
            transition_order_status(order=order, new_status=OrderStatus.CONFIRMED, force=True)
            self.assertTrue(create_shipment_for_order(order_id=order.pk))
        urlopen.assert_not_called()
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "indiapost")
        shipment = IndiaPostShipment.objects.get(order=order)
        self.assertEqual(shipment.tracking_status, ShipmentStatus.BOOKED)
        self.assertFalse(DelhiveryShipment.objects.exists())

        self.net.tracking_data = [FakeIndiaPost.track_record(shipment.barcode_number, "Item Delivered", delivered=True)]
        self.assertEqual(ip_tasks.sync_indiapost_tracking_task(), 1)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.DELIVERED)

    def test_the_delhivery_webhook_is_closed_when_delhivery_is_unconfigured(self):
        response = self.client.post(reverse("delhivery:webhook"), json.dumps({"waybill": "X1", "status": "delivered"}),
                                    content_type="application/json", HTTP_X_DELHIVERY_TOKEN="anything")
        self.assertEqual(response.status_code, 503)


# ============================================================================ switching and locking

@override_settings(**INDIA_POST_CFG, **DELHIVERY_CFG)
class SwitchingTests(CarrierTestCase):
    def _confirm(self):
        order = self.make_order(status=OrderStatus.CHECKOUT_PENDING)
        with patch("delhivery.tasks.create_shipment_for_order.delay"):
            transition_order_status(order=order, new_status=OrderStatus.CONFIRMED, force=True)
        order.refresh_from_db()
        return order

    def test_each_order_is_locked_to_the_courier_active_when_it_was_confirmed(self):
        self.use_carrier("delhivery")
        first = self._confirm()
        self.use_carrier("indiapost")
        second = self._confirm()
        self.use_carrier("delhivery")
        third = self._confirm()
        self.assertEqual([first.delivery_partner, second.delivery_partner, third.delivery_partner],
                         ["delhivery", "indiapost", "delhivery"])
        first.refresh_from_db()
        self.assertEqual(first.delivery_partner, "delhivery")  # unchanged by the later switches

    def test_dispatch_follows_the_locked_courier_not_the_current_setting(self):
        self.use_carrier("delhivery")
        order = self._confirm()
        self.use_carrier("indiapost")  # switched after confirmation
        with patch("urllib.request.urlopen", return_value=fake_response(GOOD)) as urlopen:
            create_shipment_for_order(order_id=order.pk)
        urlopen.assert_called_once()
        self.assertEqual(self.net.booking_calls(), [])
        self.assertTrue(DelhiveryShipment.objects.filter(order=order).exists())
        self.assertFalse(IndiaPostShipment.objects.filter(order=order).exists())

    def test_a_courier_is_never_booked_on_top_of_the_other(self):
        self.use_carrier("indiapost")
        order = self.make_order(partner="indiapost")
        book_shipment(order)
        with patch("urllib.request.urlopen") as urlopen:
            create_shipment_for_order(order_id=order.pk)  # asked to dispatch again
        urlopen.assert_not_called()
        self.assertFalse(DelhiveryShipment.objects.exists())

        delhivery_order = self.make_order(partner="delhivery")
        DelhiveryShipment.objects.create(order=delhivery_order, waybill_number="DL9", tracking_status="Initiated")
        from indiapost.services import IndiaPostBookingError

        with self.assertRaises(IndiaPostBookingError):
            book_shipment(delhivery_order)
        self.assertFalse(IndiaPostShipment.objects.filter(order=delhivery_order).exists())


# ============================================================================ tracking isolation

@override_settings(**INDIA_POST_CFG, **DELHIVERY_CFG)
class TrackingIsolationTests(CarrierTestCase):
    def setUp(self):
        super().setUp()
        self.dl_order = self.make_order(partner="delhivery")
        DelhiveryShipment.objects.create(order=self.dl_order, waybill_number="DL1234567890", tracking_status="Initiated")
        self.ip_order = self.make_order(partner="indiapost")
        self.ip_shipment = book_shipment(self.ip_order)

    def _status(self, order):
        order.refresh_from_db()
        return order.order_status

    def test_a_delhivery_scan_only_moves_the_delhivery_order(self):
        self.client.post(reverse("delhivery:webhook"),
                         json.dumps({"waybill": "DL1234567890", "status": "out for delivery", "status_datetime": "2026-10-01T10:00:00"}),
                         content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token")
        self.assertEqual(self._status(self.dl_order), OrderStatus.OUT_FOR_DELIVERY)
        self.assertEqual(self._status(self.ip_order), OrderStatus.CONFIRMED)

    def test_an_india_post_scan_only_moves_the_india_post_order(self):
        self.client.post(reverse("indiapost:webhook"),
                         json.dumps({"article_number": self.ip_shipment.barcode_number, "event_code": "OUT_FOR_DELIVERY"}),
                         content_type="application/json", HTTP_X_INDIAPOST_TOKEN="hook-secret")
        self.assertEqual(self._status(self.ip_order), OrderStatus.OUT_FOR_DELIVERY)
        self.assertEqual(self._status(self.dl_order), OrderStatus.CONFIRMED)

    def test_each_webhook_ignores_the_other_couriers_identifiers(self):
        india_post_scan_with_waybill = self.client.post(
            reverse("indiapost:webhook"), json.dumps({"article_number": "DL1234567890", "event_code": "DELIVERED"}),
            content_type="application/json", HTTP_X_INDIAPOST_TOKEN="hook-secret")
        self.assertEqual(json.loads(india_post_scan_with_waybill.content)["processed_events"], 0)
        delhivery_scan_with_barcode = self.client.post(
            reverse("delhivery:webhook"), json.dumps({"waybill": self.ip_shipment.barcode_number, "status": "delivered"}),
            content_type="application/json", HTTP_X_DELHIVERY_TOKEN="hook-token")
        self.assertEqual(delhivery_scan_with_barcode.status_code, 404)
        self.assertEqual(self._status(self.dl_order), OrderStatus.CONFIRMED)
        self.assertEqual(self._status(self.ip_order), OrderStatus.CONFIRMED)

    def test_the_india_post_sync_never_polls_delhivery_shipments(self):
        ip_tasks.sync_indiapost_tracking_task()
        polled = [barcode for call in self.net.tracking_calls() for barcode in call["json"]["bulk"]]
        self.assertEqual(polled, [self.ip_shipment.barcode_number])


# ============================================================================ what the admin sees

@override_settings(**INDIA_POST_CFG, **DELHIVERY_CFG)
class DashboardSectionTests(CarrierTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("adm", "a@example.com", "pw12345678"))

    def _page(self, order):
        return self.client.get(reverse("dashboard:order-detail", kwargs={"pk": order.pk})).content.decode()

    def test_a_delhivery_order_shows_only_the_delhivery_section(self):
        order = self.make_order(partner="delhivery")
        DelhiveryShipment.objects.create(order=order, waybill_number="DL1234567890", tracking_status="Initiated")
        page = self._page(order)
        self.assertIn("DL1234567890", page)
        self.assertNotIn("India Post Logistics", page)

    def test_an_india_post_order_shows_only_the_india_post_section(self):
        order = self.make_order(partner="indiapost")
        shipment = book_shipment(order)
        page = self._page(order)
        self.assertIn("India Post Logistics", page)
        self.assertIn(shipment.barcode_number, page)
        self.assertNotIn("Waybill Number", page)

    def test_the_settings_page_offers_both_couriers(self):
        page = self.client.get(reverse("dashboard:settings")).content.decode()
        self.assertIn("Delhivery Express", page)
        self.assertIn("India Post (Speed Post / Parcel)", page)
