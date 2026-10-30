"""Ship Next: payload shape, idempotent sending, failure handling, retries and routing."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import requests
from celery.exceptions import Retry
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from catalog.models import Category, Product, ProductVariant
from core.models import Currency, SiteSettings
from delhivery.models import DelhiveryShipment
from delhivery.tasks import create_shipment_for_order
from orders.models import Order, OrderItem, OrderStatus
from payments.models import PaymentStatus, PaymentTransaction
from shipnext import tasks as sn_tasks
from shipnext.models import SendStatus, ShipNextShipment
from shipnext.services import ShipNextError, build_payload, needs_initiation, order_weight_grams, send_order

URL = "https://ship-next.com/api/17/order-updates"
ADDRESS = {
    "name": "Ravi K", "phone": "9876543210", "line1": "12 MG Road", "line2": "Aluva H.O",
    "city": "Kochi", "state": "Kerala", "pincode": "682001",
}


def reply(status=201, body=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = {"success": True, "message": "ok", "order_id": 6955} if body is None else body
    return response


@override_settings(SHIPNEXT_WEBHOOK_URL=URL)
class ShipNextTestCase(TestCase):
    seq = 0

    def setUp(self):
        self.currency, _ = Currency.objects.get_or_create(
            code="INR", defaults={"symbol": "₹", "exchange_rate_to_base": "1", "is_default": True}
        )
        category = Category.objects.create(name="Gym", slug="gym")
        self.hoodie = Product.objects.create(
            name="Hoodie", slug="hoodie", sku="H1", category=category, base_price="500", mrp="500",
            purchase_price="300", stock_quantity=20, weight="0.400",
        )
        self.hoodie_variant = ProductVariant.objects.create(
            product=self.hoodie, variant_type="size", name="Black / L", sku_suffix="L BLK HOOD", stock_quantity=20
        )
        self.cap = Product.objects.create(
            name="Cap", slug="cap", sku="C1", category=category, base_price="200", mrp="200",
            purchase_price="100", stock_quantity=20, weight="0.150",
        )

    def make_order(self, *, cod=False, address=None, status=OrderStatus.CONFIRMED, items=None):
        ShipNextTestCase.seq += 1
        order = Order.objects.create(
            order_number=f"#S{ShipNextTestCase.seq}", order_status=status, success_at=timezone.now(),
            subtotal=Decimal("1200"), total_amount=Decimal("1200"), currency=self.currency,
            delivery_address_snapshot=ADDRESS if address is None else address,
        )
        for product, quantity, variant in items or [(self.hoodie, 2, self.hoodie_variant), (self.cap, 1, None)]:
            OrderItem.objects.create(
                order=order, product=product, quantity=quantity, unit_price=product.base_price,
                variant=variant, variant_name=variant.name if variant else "",
            )
        PaymentTransaction.objects.create(
            order=order, gateway_key="cod" if cod else "razorpay_upi", amount=order.total_amount,
            currency=self.currency, status=PaymentStatus.PENDING if cod else PaymentStatus.SUCCESS,
        )
        return order


class PayloadTests(ShipNextTestCase):
    def test_follows_the_ship_next_format(self):
        payload = build_payload(self.make_order())
        self.assertTrue(payload["name"].startswith("#S"))  # Shopify-style: the order number, # included
        self.assertEqual(payload["total_price"], 1200.0)
        self.assertEqual(payload["financial_status"], "Paid")
        self.assertEqual(payload["shipping_address"], {
            "name": "Ravi K", "phone": "9876543210", "address1": "12 MG Road", "address2": "Aluva H.O",
            "city": "Kochi", "province": "Kerala", "zip": "682001", "country": "India",
        })
        self.assertEqual(payload["line_items"][0], {
            "order_id": payload["name"], "name": "L BLK HOOD", "price": 500.0, "fulfillable_quantity": 2,
        })
        self.assertEqual(payload["line_items"][1]["name"], "C1")
        self.assertNotIn("shipping_phone", payload)
        self.assertNotIn("shipping_zip", payload)

    def test_a_line_is_named_by_its_variant_sku_falling_back_to_the_product_sku(self):
        def names(variant):
            order = self.make_order(items=[(self.hoodie, 1, variant)])
            return [line["name"] for line in build_payload(order)["line_items"]]

        self.assertEqual(names(self.hoodie_variant), ["L BLK HOOD"])
        self.assertEqual(names(None), ["H1"])  # no variant
        for unset in ("", "  ", "0"):  # variant without a SKU ("0" is the catalog's "not set")
            ProductVariant.objects.filter(pk=self.hoodie_variant.pk).update(sku_suffix=unset)
            self.hoodie_variant.refresh_from_db()
            self.assertEqual(names(self.hoodie_variant), ["H1"], repr(unset))
        ProductVariant.objects.filter(pk=self.hoodie_variant.pk).update(sku_suffix="  M DST BLK  ")
        self.hoodie_variant.refresh_from_db()
        self.assertEqual(names(self.hoodie_variant), ["M DST BLK"])

    def test_cod_is_pending_and_prepaid_is_paid(self):
        self.assertEqual(build_payload(self.make_order(cod=True))["financial_status"], "pending")
        self.assertEqual(build_payload(self.make_order(cod=False))["financial_status"], "Paid")

    def test_weight_is_total_grams_across_lines_with_a_500_g_default(self):
        self.assertEqual(order_weight_grams(self.make_order()), 400 * 2 + 150)
        self.hoodie.weight = None
        self.hoodie.save()
        self.cap.weight = None
        self.cap.save()
        self.assertEqual(order_weight_grams(self.make_order()), 500)
        self.assertIsInstance(build_payload(self.make_order())["total_weight"], int)

    def test_package_size_and_shape(self):
        #no product declares a size: India Post's parcel minimum, as a box
        payload = build_payload(self.make_order())
        self.assertEqual(
            (payload["pkg_length"], payload["pkg_breadth"], payload["pkg_height"], payload["pkg_shape"]),
            (14, 9, 1, "nrol"),
        )
        #the largest declared side wins, rounded up to whole cm
        Product.objects.filter(pk=self.hoodie.pk).update(length="30.2", width="22", height="4")
        Product.objects.filter(pk=self.cap.pk).update(length="20", width="25.5", height="2")
        payload = build_payload(self.make_order())
        self.assertEqual((payload["pkg_length"], payload["pkg_breadth"], payload["pkg_height"]), (31, 26, 4))
        self.assertTrue(all(isinstance(payload[key], int) for key in ("pkg_length", "pkg_breadth", "pkg_height")))

    def test_bad_delivery_data_is_a_permanent_error(self):
        order = self.make_order(address={**ADDRESS, "pincode": "123"})
        with self.assertRaises(ShipNextError) as ctx:
            build_payload(order)
        self.assertFalse(ctx.exception.retryable)


class SendTests(ShipNextTestCase):
    def test_success_records_the_ship_next_order_and_locks_the_courier(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply()) as post:
            shipment = send_order(order)
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], URL)
        self.assertEqual(post.call_args.kwargs["json"]["name"], order.order_number)
        self.assertEqual((shipment.send_status, shipment.external_order_id, shipment.attempts), (SendStatus.SENT, "6955", 1))
        self.assertIsNotNone(shipment.sent_at)
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "shipnext")
        self.assertEqual(order.carrier_display_name, "Ship Next")

    def test_the_tracking_number_in_the_response_is_saved_and_drives_the_track_link(self):
        order = self.make_order()
        body = {
            "success": True, "message": "Order processed successfully", "order_id": 7091,
            "tracking_number": "EY453950670IN", "shipping_status": "pickup/drop-off waiting",
        }
        with patch("shipnext.services.requests.post", return_value=reply(201, body)):
            shipment = send_order(order)
        self.assertEqual(shipment.tracking_number, "EY453950670IN")
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "EY453950670IN")
        self.assertEqual(order.tracking_url, "https://ship-next.com/trace-order/EY453950670IN")

    def test_there_is_no_track_link_until_a_tracking_number_exists(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply()):  # no tracking_number in the reply
            send_order(order)
        order.refresh_from_db()
        self.assertEqual(order.tracking_url, "")

    def test_a_reply_without_tracking_never_blanks_a_saved_number(self):
        order = self.make_order()
        ShipNextShipment.objects.create(order=order, tracking_number="EM1IN")
        with patch("shipnext.services.requests.post", return_value=reply()):
            send_order(order)
        self.assertEqual(ShipNextShipment.objects.get(order=order).tracking_number, "EM1IN")

    def test_other_couriers_keep_their_own_tracking_pages(self):
        order = self.make_order()
        order.delivery_partner = "delhivery"
        DelhiveryShipment.objects.create(order=order, waybill_number="DL1", tracking_status="Initiated")
        self.assertEqual(order.tracking_url, "https://www.delhivery.com/track-v2/package/DL1")

    def test_sending_twice_posts_once(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply()) as post:
            send_order(order)
            send_order(order)
        post.assert_called_once()
        self.assertEqual(ShipNextShipment.objects.get(order=order).attempts, 1)

    def test_server_error_is_retryable_and_a_later_attempt_succeeds(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply(500, {"message": "Server Error"})):
            with self.assertRaises(ShipNextError) as ctx:
                send_order(order)
        self.assertTrue(ctx.exception.retryable)
        shipment = ShipNextShipment.objects.get(order=order)
        self.assertEqual(shipment.send_status, SendStatus.FAILED)
        self.assertIn("Server Error", shipment.error_message)
        with patch("shipnext.services.requests.post", return_value=reply()):
            send_order(order)
        shipment.refresh_from_db()
        self.assertEqual((shipment.send_status, shipment.attempts, shipment.error_message), (SendStatus.SENT, 2, ""))

    def test_rejection_is_permanent(self):
        order = self.make_order()
        for status, body in ((422, {"message": "bad"}), (200, {"success": False, "message": "no"})):
            with patch("shipnext.services.requests.post", return_value=reply(status, body)):
                with self.assertRaises(ShipNextError) as ctx:
                    send_order(order)
            self.assertFalse(ctx.exception.retryable)

    def test_rate_limit_is_retryable(self):
        with patch("shipnext.services.requests.post", return_value=reply(429, {})):
            with self.assertRaises(ShipNextError) as ctx:
                send_order(self.make_order())
        self.assertTrue(ctx.exception.retryable)

    def test_network_error_is_retryable(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(ShipNextError) as ctx:
                send_order(order)
        self.assertTrue(ctx.exception.retryable)
        self.assertEqual(ShipNextShipment.objects.get(order=order).send_status, SendStatus.FAILED)

    def test_a_non_json_reply_is_a_failure_not_a_crash(self):
        response = reply(200)
        response.json.side_effect = ValueError("not json")
        response.text = "<html>oops</html>"
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=response):
            with self.assertRaises(ShipNextError):
                send_order(order)
        self.assertEqual(ShipNextShipment.objects.get(order=order).send_status, SendStatus.FAILED)

    def test_an_unexpected_bug_never_leaves_the_claim_dangling(self):
        order = self.make_order()
        with patch("shipnext.services.build_payload", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                send_order(order)
        self.assertEqual(ShipNextShipment.objects.get(order=order).send_status, SendStatus.FAILED)

    def test_bad_delivery_data_sends_nothing(self):
        order = self.make_order(address={**ADDRESS, "phone": "123"})
        with patch("shipnext.services.requests.post") as post:
            with self.assertRaises(ShipNextError):
                send_order(order)
        post.assert_not_called()
        self.assertEqual(ShipNextShipment.objects.get(order=order).send_status, SendStatus.FAILED)

    @override_settings(SHIPNEXT_WEBHOOK_URL="")
    def test_missing_url_fails_loudly_without_a_request(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post") as post:
            with self.assertRaises(ShipNextError):
                send_order(order)
        post.assert_not_called()
        self.assertFalse(ShipNextShipment.objects.exists())

    def test_cancelled_orders_are_not_sent(self):
        with patch("shipnext.services.requests.post") as post:
            with self.assertRaises(ShipNextError):
                send_order(self.make_order(status=OrderStatus.CANCELLED))
        post.assert_not_called()

    def test_an_order_booked_with_another_courier_is_not_sent(self):
        order = self.make_order()
        DelhiveryShipment.objects.create(order=order, waybill_number="DL1", tracking_status="Initiated")
        with patch("shipnext.services.requests.post") as post:
            with self.assertRaises(ShipNextError):
                send_order(order)
        post.assert_not_called()

    def test_a_send_in_progress_blocks_a_second_one_until_the_claim_goes_stale(self):
        order = self.make_order()
        shipment = ShipNextShipment.objects.create(order=order, send_status=SendStatus.SENDING)
        with patch("shipnext.services.requests.post", return_value=reply()) as post:
            with self.assertRaises(ShipNextError) as ctx:
                send_order(order)
            self.assertTrue(ctx.exception.retryable)
            post.assert_not_called()
            ShipNextShipment.objects.filter(pk=shipment.pk).update(updated_at=timezone.now() - timedelta(minutes=11))
            send_order(order)
        post.assert_called_once()


class TaskAndRoutingTests(ShipNextTestCase):
    def test_transient_failure_schedules_a_retry_with_backoff(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply(503, {})):
            with patch.object(sn_tasks.send_order_to_shipnext, "retry", side_effect=Retry()) as retry:
                with self.assertRaises(Retry):
                    sn_tasks.send_order_to_shipnext.run(order.pk)
        self.assertEqual(retry.call_args.kwargs["countdown"], 60)

    def test_permanent_failure_is_recorded_and_not_retried(self):
        order = self.make_order(address={**ADDRESS, "pincode": "123"})
        with patch.object(sn_tasks.send_order_to_shipnext, "retry") as retry:
            self.assertFalse(sn_tasks.send_order_to_shipnext.run(order.pk))
        retry.assert_not_called()
        self.assertEqual(ShipNextShipment.objects.get(order=order).send_status, SendStatus.FAILED)

    def test_unknown_order_is_reported_not_raised(self):
        self.assertFalse(sn_tasks.send_order_to_shipnext.run(999999))

    def test_a_ship_next_order_routes_to_the_ship_next_task_only(self):
        order = self.make_order()
        order.delivery_partner = "shipnext"
        order.save(update_fields=["delivery_partner"])
        with patch("shipnext.tasks.send_order_to_shipnext.delay") as delay, \
                patch("indiapost.tasks.create_indiapost_shipment_for_order.delay") as india_post, \
                patch("delhivery.services.trigger_shipment_on_confirmed") as delhivery:
            self.assertTrue(create_shipment_for_order(order_id=order.pk))
        delay.assert_called_once_with(order_id=order.pk)
        india_post.assert_not_called()
        delhivery.assert_not_called()

    def test_confirming_locks_the_order_to_the_active_ship_next_setting(self):
        from orders.services import transition_order_status

        SiteSettings.objects.first() or SiteSettings.objects.create()
        SiteSettings.objects.update(shipping_carrier="shipnext")
        order = self.make_order(status=OrderStatus.CHECKOUT_PENDING)
        with patch("delhivery.tasks.create_shipment_for_order.delay"):
            transition_order_status(order=order, new_status=OrderStatus.CONFIRMED, force=True)
        order.refresh_from_db()
        self.assertEqual(order.delivery_partner, "shipnext")


class DashboardTests(ShipNextTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_user("staff", "s@example.com", "pw", is_staff=True, is_superuser=True)
        self.client.force_login(self.staff)

    def test_tracking_number_is_saved_by_hand_and_shown_on_the_order(self):
        order = self.make_order()
        order.delivery_partner = "shipnext"
        order.save(update_fields=["delivery_partner"])
        self.client.post(reverse("dashboard:order-tracking-update", args=[order.pk]), {"waybill_number": "EM123456789IN"})
        order.refresh_from_db()
        self.assertEqual(order.tracking_number, "EM123456789IN")
        self.assertIn("EM123456789IN", order.tracking_url)
        self.assertFalse(DelhiveryShipment.objects.filter(order=order).exists())

    def test_resend_sends_and_reports_the_result(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply()):
            response = self.client.post(reverse("dashboard:order-shipnext-send", args=[order.pk]), follow=True)
        self.assertContains(response, "Order sent to Ship Next")
        self.assertTrue(ShipNextShipment.objects.get(order=order).is_sent)

    def test_resend_shows_the_failure_reason(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply(500, {"message": "Server Error"})):
            response = self.client.post(reverse("dashboard:order-shipnext-send", args=[order.pk]), follow=True)
        self.assertContains(response, "Server Error")

    def test_an_order_locked_to_another_courier_cannot_be_sent(self):
        order = self.make_order()
        order.delivery_partner = "delhivery"
        order.save(update_fields=["delivery_partner"])
        with patch("shipnext.services.requests.post") as post:
            self.client.post(reverse("dashboard:order-shipnext-send", args=[order.pk]))
        post.assert_not_called()

    def test_the_order_page_shows_the_ship_next_panel(self):
        order = self.make_order()
        with patch("shipnext.services.requests.post", return_value=reply(500, {"message": "Server Error"})):
            self.client.post(reverse("dashboard:order-shipnext-send", args=[order.pk]))
        response = self.client.get(reverse("dashboard:order-detail", args=[order.pk]))
        self.assertContains(response, "Ship Next Logistics")
        self.assertContains(response, "Send to Ship Next")

    def test_the_ajax_send_returns_the_outcome_as_json(self):
        order = self.make_order()
        url = reverse("dashboard:order-shipnext-send", args=[order.pk])
        with patch("shipnext.services.requests.post", return_value=reply()):
            data = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertEqual((data["ok"], data["status"], data["order_id"]), (True, "Sent", "6955"))

    def test_the_ajax_send_reports_the_exact_issue(self):
        order = self.make_order()
        url = reverse("dashboard:order-shipnext-send", args=[order.pk])
        body = {"message": "Insufficient wallet balance", "errors": {"zip": ["Not serviceable"]}}
        with patch("shipnext.services.requests.post", return_value=reply(422, body)):
            data = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "Failed")
        self.assertIn("Insufficient wallet balance", data["message"])
        self.assertIn("Not serviceable", data["message"])

    def test_the_ajax_send_for_another_couriers_order_is_refused(self):
        order = self.make_order()
        order.delivery_partner = "indiapost"
        order.save(update_fields=["delivery_partner"])
        url = reverse("dashboard:order-shipnext-send", args=[order.pk])
        with patch("shipnext.services.requests.post") as post:
            data = self.client.post(url, HTTP_X_REQUESTED_WITH="XMLHttpRequest").json()
        post.assert_not_called()
        self.assertFalse(data["ok"])

    def test_the_order_list_marks_sent_orders_and_offers_reinitiate_on_failed_ones(self):
        sent = self.make_order()
        failed = self.make_order()
        untouched = self.make_order()
        for order in (sent, failed, untouched):
            order.delivery_partner = "shipnext"
            order.save(update_fields=["delivery_partner"])
        with patch("shipnext.services.requests.post", return_value=reply()):
            send_order(sent)
        with patch("shipnext.services.requests.post", return_value=reply(500, {"message": "Server Error"})):
            with self.assertRaises(ShipNextError):
                send_order(failed)

        html = self.client.get(reverse("dashboard:order-list")).content.decode()
        row = lambda order: html.split(f">{order.order_number}</a>")[1].split("</tr>")[0]
        self.assertIn("Initiated · India Post", row(sent))
        self.assertNotIn("js-reinitiate", row(sent))
        self.assertIn("Delivery failed", row(failed))
        self.assertIn("Reinitiate", row(failed))
        self.assertIn("Not initiated", row(untouched))
        self.assertRegex(row(untouched), r"</i>\s*Initiate\s*</button>")


class NeedsInitiationTests(ShipNextTestCase):
    def test_only_confirmed_unsent_ship_next_orders_need_initiation(self):
        order = self.make_order()
        self.assertFalse(needs_initiation(order))  # not locked to Ship Next
        order.delivery_partner = "shipnext"
        self.assertTrue(needs_initiation(order))
        for status in (OrderStatus.CHECKOUT_PENDING, OrderStatus.PLACED_COD, OrderStatus.CANCELLED, OrderStatus.REFUNDED):
            order.order_status = status
            self.assertFalse(needs_initiation(order), status)
        order.order_status = OrderStatus.CONFIRMED
        with patch("shipnext.services.requests.post", return_value=reply()):
            send_order(order)
        order.refresh_from_db()
        self.assertFalse(needs_initiation(order))



class CallbackTests(ShipNextTestCase):
    """Ship Next calls /shipnext/webhook/<order number>/ with a status and/or tracking number."""

    def post(self, order, body=None, ref=None, raw=None, **extra):
        if ref is None:
            ref = order.order_number.lstrip("#")
        data = raw if raw is not None else json.dumps(body or {})
        return self.client.post(reverse("shipnext:webhook", args=[ref]), data, content_type="application/json", **extra)

    def shipnext_order(self, status=OrderStatus.CONFIRMED):
        order = self.make_order(status=status)
        order.delivery_partner = "shipnext"
        order.save(update_fields=["delivery_partner"])
        return order

    def test_the_url_structure_and_a_fulfilled_response(self):
        order = self.shipnext_order()
        number = order.order_number.lstrip("#")
        self.assertEqual(reverse("shipnext:webhook", args=[number]), f"/shipnext/webhook/{number}/")
        response = self.post(order, {"status": "fulfilled", "tracking_number": "EM123456789IN"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "success": True,
            "order": order.order_number,
            "order_status": "ready_to_ship",
            "tracking_number": "EM123456789IN",
            "message": f"Order {order.order_number} updated: Ready to Ship.",
        })
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.READY_TO_SHIP)
        self.assertEqual(order.tracking_number, "EM123456789IN")
        self.assertIn("EM123456789IN", order.tracking_url)
        self.assertTrue(ShipNextShipment.objects.get(order=order).is_sent)

    def test_the_hash_in_the_order_number_is_optional(self):
        order = self.shipnext_order()
        url = f"/shipnext/webhook/%23{order.order_number.lstrip('#')}/"  # "#3001" with the # URL-encoded
        response = self.client.post(url, json.dumps({"status": "in_transit"}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.IN_TRANSIT)

    def test_a_tracking_number_alone_means_fulfilled(self):
        order = self.shipnext_order()
        self.assertEqual(self.post(order, {"tracking_number": "EM1IN"}).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.READY_TO_SHIP)

    def test_later_updates_move_the_order_forward_to_delivered(self):
        order = self.shipnext_order()
        for status, expected in (
            ("in transit", OrderStatus.IN_TRANSIT),
            ("Out for delivery", OrderStatus.OUT_FOR_DELIVERY),
            ("delivered", OrderStatus.DELIVERED),
        ):
            self.assertEqual(self.post(order, {"status": status}).json()["order_status"], expected)
            order.refresh_from_db()
            self.assertEqual(order.order_status, expected)

    def test_a_late_or_repeated_update_never_moves_the_order_backwards(self):
        order = self.shipnext_order(status=OrderStatus.OUT_FOR_DELIVERY)
        response = self.post(order, {"status": "in_transit"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["order_status"], "out_for_delivery")
        self.assertIn("status not changed", response.json()["message"])
        self.post(order, {"status": "out_for_delivery"})
        self.assertEqual(order.status_history.count(), 0)

    def test_a_return_cancels_a_live_order_but_never_a_delivered_one(self):
        live = self.shipnext_order()
        self.post(live, {"status": "rto"})
        live.refresh_from_db()
        self.assertEqual(live.order_status, OrderStatus.CANCELLED)

        delivered = self.shipnext_order(status=OrderStatus.DELIVERED)
        self.assertEqual(self.post(delivered, {"status": "returned"}).json()["order_status"], "delivered")

    def test_a_cancelled_order_is_not_revived(self):
        order = self.shipnext_order(status=OrderStatus.CANCELLED)
        self.assertEqual(self.post(order, {"status": "fulfilled"}).json()["order_status"], "cancelled")

    def test_an_order_from_another_courier_is_refused_and_untouched(self):
        other = self.make_order()
        other.delivery_partner = "delhivery"
        other.save(update_fields=["delivery_partner"])
        response = self.post(other, {"status": "delivered", "tracking_number": "X1"})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()["success"])
        other.refresh_from_db()
        self.assertEqual(other.order_status, OrderStatus.CONFIRMED)
        self.assertFalse(ShipNextShipment.objects.filter(order=other).exists())

    def test_an_unknown_order_is_a_404(self):
        response = self.client.post(reverse("shipnext:webhook", args=["999999"]), "{}", content_type="application/json")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"success": False, "message": "Order #999999 was not found."})

    def test_bad_input_is_a_clear_400_and_changes_nothing(self):
        order = self.shipnext_order()
        for body, text in (
            ({"status": "teleported"}, "Unknown status 'teleported'"),
            ({}, "Send a status and/or a tracking_number"),
        ):
            response = self.post(order, body)
            self.assertEqual(response.status_code, 400)
            self.assertIn(text, response.json()["message"])
        for raw in ("not json", "[1, 2]"):
            self.assertEqual(self.post(order, raw=raw).status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.CONFIRMED)
        self.assertFalse(ShipNextShipment.objects.filter(order=order).exists())

    def test_form_encoded_updates_work_too(self):
        order = self.shipnext_order()
        response = self.client.post(
            reverse("shipnext:webhook", args=[order.order_number.lstrip("#")]),
            {"status": "delivered", "tracking_number": "EM9IN"},
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.DELIVERED)

    def test_get_is_not_allowed(self):
        order = self.shipnext_order()
        response = self.client.get(reverse("shipnext:webhook", args=[order.order_number.lstrip("#")]))
        self.assertEqual(response.status_code, 405)


class CheckoutPostOfficeTests(ShipNextTestCase):
    """With Ship Next as the courier, checkout picks the Post Office from a list fetched for the PIN."""

    def set_carrier(self, carrier):
        SiteSettings.objects.first() or SiteSettings.objects.create()
        SiteSettings.objects.update(shipping_carrier=carrier)

    def page(self):
        self.client.post(reverse("cart:add"), {"product_id": self.cap.pk, "quantity": 1}, HTTP_HX_REQUEST="true")
        return self.client.get(reverse("checkout:checkout")).content.decode()

    def place(self, **overrides):
        data = {
            "gateway_key": "cod", "idempotency_key": "k", "guest_name": "Ravi K", "guest_email": "r@example.com",
            "guest_phone": "9876543210", "guest_address_line1": "12 MG Road", "guest_address_line2": "",
            "guest_city": "Kochi", "guest_state": "Kerala", "guest_pincode": "682030", **overrides,
        }
        return self.client.post(reverse("checkout:place-order"), data, HTTP_HX_REQUEST="true").content.decode()

    def test_ship_next_shows_a_post_office_dropdown_that_waits_for_the_pin(self):
        self.set_carrier("shipnext")
        html = self.page()
        self.assertIn('id="post-office-select"', html)
        self.assertIn("api.postalpincode.in/pincode/", html)
        self.assertIn("var postOfficeMode = true;", html)
        self.assertNotIn("Address Line 2 (Optional)", html)
        select = html.split('id="post-office-select"')[1].split(">")[0]
        self.assertIn("disabled", select)  # nothing to choose until a complete PIN has been looked up

    def test_other_carriers_do_not_get_the_dropdown(self):
        for carrier, optional_line2 in (("delhivery", True), ("indiapost", False)):
            self.set_carrier(carrier)
            html = self.page()
            self.assertNotIn('id="post-office-select"', html, carrier)
            self.assertNotIn("api.postalpincode.in", html, carrier)
            self.assertEqual("Address Line 2 (Optional)" in html, optional_line2, carrier)

    def test_the_post_office_is_required_server_side_with_ship_next(self):
        self.set_carrier("shipnext")
        self.page()
        self.assertIn("Post Office is required.", self.place())
        self.assertIn("valid post office name", self.place(guest_address_line2="12345"))
        self.assertNotIn("Post Office is required.", self.place(guest_address_line2="Kakkanad"))

    def test_the_chosen_post_office_reaches_ship_next_as_address_line_2(self):
        self.set_carrier("shipnext")
        self.page()
        self.place(guest_address_line2="Kakkanad West")
        order = Order.objects.exclude(order_status=OrderStatus.CHECKOUT_PENDING).filter(
            delivery_address_snapshot__line2="Kakkanad West"
        ).first()
        self.assertIsNotNone(order)
        self.assertEqual(build_payload(order)["shipping_address"]["address2"], "Kakkanad West")
