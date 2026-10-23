"""Tests for customer returns / replacements: eligibility, requests, approval, pickup, close-out, dashboard."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts.services import ensure_customer_profile_for_user
from catalog.models import Category, Product, ProductVariant
from core.models import Currency, SiteSettings
from indiapost.models import IndiaPostBarcodePool
from indiapost.tests import CFG as INDIA_POST_CFG, FakeIndiaPost
from orders.models import Order, OrderItem, OrderStatus, OrderStatusHistory
from returns import services
from returns.models import ReturnRequest, ReturnStatus
from returns.services import ReturnError


def _day(offset=0):
    return timezone.localdate() + timedelta(days=offset)


@override_settings(**INDIA_POST_CFG, EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ReturnsTestCase(TestCase):
    seq = 0

    def setUp(self):
        self.net = FakeIndiaPost()
        patcher = patch("indiapost.client.requests.request", self.net.request)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.currency, _ = Currency.objects.get_or_create(
            code="INR", defaults={"symbol": "₹", "exchange_rate_to_base": "1", "is_default": True}
        )
        site = SiteSettings.objects.first() or SiteSettings.objects.create()
        site.return_window_days = 7
        site.shipping_carrier = "delhivery"
        site.order_notification_email = "store@example.com"
        site.save()

        self.user = User.objects.create_user("buyer", "buyer@example.com", "pw12345678", first_name="Asha", last_name="Nair")
        self.profile = ensure_customer_profile_for_user(user=self.user)
        self.profile.phone = "9876543210"
        self.profile.save(update_fields=["phone"])
        category = Category.objects.create(name="Gym", slug="gym")
        self.hoodie = Product.objects.create(
            name="Hoodie", slug="hoodie", sku="H1", category=category, base_price="500", mrp="500",
            purchase_price="300", stock_quantity=10, weight="0.400",
        )
        self.pants = Product.objects.create(
            name="Pants", slug="pants", sku="P1", category=category, base_price="300", mrp="300",
            purchase_price="150", stock_quantity=10, weight="0.300",
        )
        IndiaPostBarcodePool.objects.create(prefix="ET", current_number=21433001, end_number=21433050, suffix="IN")

    def make_order(self, *, delivered_days_ago=1, status=OrderStatus.DELIVERED, profile=None, partner="delhivery"):
        ReturnsTestCase.seq += 1
        order = Order.objects.create(
            customer_profile=profile or self.profile,
            order_number=f"#R{ReturnsTestCase.seq}",
            order_status=status,
            success_at=timezone.now(),
            subtotal=Decimal("1100"),
            total_amount=Decimal("1100"),
            currency=self.currency,
            delivery_partner=partner,
            delivery_address_snapshot={
                "name": "Ravi K", "phone": "9876543210", "email": "ravi@example.com", "line1": "12 MG Road",
                "line2": "Aluva H.O", "city": "Kochi", "state": "Kerala", "pincode": "682001",
            },
        )
        self.hoodie_line = OrderItem.objects.create(order=order, product=self.hoodie, quantity=2, unit_price=Decimal("500"))
        self.pants_line = OrderItem.objects.create(order=order, product=self.pants, quantity=1, unit_price=Decimal("100"))
        if status == OrderStatus.DELIVERED:
            entry = OrderStatusHistory.objects.create(order=order, from_status="out_for_delivery", to_status="delivered")
            OrderStatusHistory.objects.filter(pk=entry.pk).update(changed_at=timezone.now() - timedelta(days=delivered_days_ago))
        return order

    def request_return(self, order, *, hoodie_qty=1, pants_qty=0, reason="size_fit", resolution="refund", note=""):
        selections = []
        if hoodie_qty:
            selections.append((self.hoodie_line.pk, hoodie_qty, reason))
        if pants_qty:
            selections.append((self.pants_line.pk, pants_qty, reason))
        return services.create_return_request(
            order=order, customer_profile=self.profile, selections=selections, resolution=resolution, note=note
        )

    def approved_india_post(self, **kwargs):
        return services.approve_return(self.request_return(self.make_order(partner="indiapost"), **kwargs))


# ============================================================================ eligibility

class EligibilityTests(ReturnsTestCase):
    def test_delivered_order_inside_the_window_is_eligible(self):
        eligibility = services.get_eligibility(self.make_order(delivered_days_ago=3))
        self.assertTrue(eligibility.allowed)
        self.assertEqual({e.order_item.pk: e.available for e in eligibility.items}, {self.hoodie_line.pk: 2, self.pants_line.pk: 1})
        self.assertEqual(eligibility.deadline, _day(4))

    def test_last_day_of_the_window_is_inclusive_and_the_next_day_is_closed(self):
        self.assertTrue(services.get_eligibility(self.make_order(delivered_days_ago=7)).allowed)
        closed = services.get_eligibility(self.make_order(delivered_days_ago=8))
        self.assertFalse(closed.allowed)
        self.assertIn("7-day return window", closed.message)

    def test_window_comes_from_site_settings(self):
        SiteSettings.objects.update(return_window_days=30)
        self.assertTrue(services.get_eligibility(self.make_order(delivered_days_ago=20)).allowed)
        SiteSettings.objects.update(return_window_days=2)
        self.assertFalse(services.get_eligibility(self.make_order(delivered_days_ago=3)).allowed)

    def test_zero_turns_returns_off(self):
        SiteSettings.objects.update(return_window_days=0)
        self.assertFalse(services.get_eligibility(self.make_order()).allowed)

    def test_undelivered_orders_are_not_eligible(self):
        for status in (OrderStatus.CONFIRMED, OrderStatus.IN_TRANSIT, OrderStatus.CANCELLED, OrderStatus.CHECKOUT_PENDING):
            self.assertFalse(services.get_eligibility(self.make_order(status=status)).allowed, status)

    def test_quantities_already_in_open_requests_are_not_returnable_again(self):
        order = self.make_order()
        self.request_return(order, hoodie_qty=1)
        self.assertEqual({e.order_item.pk: e.available for e in services.get_eligibility(order).items}[self.hoodie_line.pk], 1)

    def test_rejected_and_cancelled_requests_release_their_quantity(self):
        order = self.make_order()
        first = self.request_return(order, hoodie_qty=2)
        self.assertEqual(len(services.get_eligibility(order).items), 1)  # only pants left
        services.reject_return(first, reason="Worn")
        self.assertEqual({e.order_item.pk: e.available for e in services.get_eligibility(order).items}[self.hoodie_line.pk], 2)
        second = self.request_return(order, hoodie_qty=2)
        services.cancel_return_request(second, customer_profile=self.profile)
        self.assertEqual({e.order_item.pk: e.available for e in services.get_eligibility(order).items}[self.hoodie_line.pk], 2)

    def test_fully_returned_order_is_no_longer_eligible(self):
        order = self.make_order()
        self.request_return(order, hoodie_qty=2, pants_qty=1)
        eligibility = services.get_eligibility(order)
        self.assertFalse(eligibility.allowed)
        self.assertIn("already has a return request", eligibility.message)


# ============================================================================ customer requests

class CreateRequestTests(ReturnsTestCase):
    def test_creates_a_request_per_product_with_its_own_quantity_and_reason(self):
        order = self.make_order()
        request = services.create_return_request(
            order=order, customer_profile=self.profile,
            selections=[(self.hoodie_line.pk, 1, "size_fit"), (self.pants_line.pk, 1, "damaged")], note=" too tight ",
        )
        self.assertEqual(request.status, ReturnStatus.REQUESTED)
        self.assertEqual(request.return_number, f"RET-{order.order_number.lstrip('#')}-1")
        self.assertEqual(request.customer_note, "too tight")
        self.assertEqual({(i.order_item_id, i.quantity, i.reason) for i in request.items.all()},
                         {(self.hoodie_line.pk, 1, "size_fit"), (self.pants_line.pk, 1, "damaged")})

    def test_resolution_defaults_to_refund_and_replacement_is_recorded(self):
        self.assertEqual(self.request_return(self.make_order()).resolution, "refund")
        self.assertEqual(self.request_return(self.make_order(), resolution="replacement").resolution, "replacement")

    def test_invalid_resolution_is_refused(self):
        with self.assertRaises(ReturnError):
            self.request_return(self.make_order(), resolution="store-credit")

    def test_the_delivering_courier_is_recorded_on_the_request(self):
        self.assertEqual(self.request_return(self.make_order(partner="indiapost")).carrier, "indiapost")
        self.assertEqual(self.request_return(self.make_order(partner="delhivery")).carrier, "delhivery")
        SiteSettings.objects.update(shipping_carrier="indiapost")
        self.assertEqual(self.request_return(self.make_order(partner=None)).carrier, "indiapost")

    def test_second_request_gets_the_next_number(self):
        order = self.make_order()
        self.request_return(order, hoodie_qty=1)
        self.assertTrue(self.request_return(order, hoodie_qty=1).return_number.endswith("-2"))

    def test_outside_the_window_is_refused_server_side(self):
        with self.assertRaises(ReturnError):
            self.request_return(self.make_order(delivered_days_ago=30))
        self.assertFalse(ReturnRequest.objects.exists())

    def test_quantity_cannot_exceed_what_was_bought_or_what_is_left(self):
        order = self.make_order()
        with self.assertRaises(ReturnError):
            self.request_return(order, hoodie_qty=3)
        self.request_return(order, hoodie_qty=2)
        with self.assertRaises(ReturnError):
            self.request_return(order, hoodie_qty=1)

    def test_validation_errors(self):
        order = self.make_order()
        with self.assertRaises(ReturnError):
            services.create_return_request(order=order, customer_profile=self.profile, selections=[], note="")
        with self.assertRaises(ReturnError):
            self.request_return(order, hoodie_qty=1, reason="because")
        with self.assertRaises(ReturnError):
            services.create_return_request(order=order, customer_profile=self.profile, selections=[(999999, 1, "damaged")])
        with self.assertRaises(ReturnError):
            services.create_return_request(
                order=order, customer_profile=self.profile,
                selections=[(self.hoodie_line.pk, 1, "damaged"), (self.hoodie_line.pk, 1, "damaged")],
            )

    def test_another_customers_order_is_refused(self):
        other = ensure_customer_profile_for_user(user=User.objects.create_user("other", "o@example.com", "pw12345678"))
        order = self.make_order()
        with self.assertRaises(ReturnError):
            services.create_return_request(order=order, customer_profile=other, selections=[(self.hoodie_line.pk, 1, "damaged")])

    def test_customer_and_store_are_emailed(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.request_return(self.make_order())
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["buyer@example.com", "store@example.com"])

    def test_customer_can_cancel_only_before_approval(self):
        request = self.request_return(self.make_order())
        services.cancel_return_request(request, customer_profile=self.profile)
        self.assertEqual(request.status, ReturnStatus.CANCELLED)
        with self.assertRaises(ReturnError):
            services.cancel_return_request(request, customer_profile=self.profile)
        approved = services.approve_return(self.request_return(self.make_order()))
        with self.assertRaises(ReturnError):
            services.cancel_return_request(approved, customer_profile=self.profile)


# ============================================================================ customer pages

class CustomerPageTests(ReturnsTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _post(self, order, **extra):
        data = {
            f"qty_{self.hoodie_line.pk}": "1", f"reason_{self.hoodie_line.pk}": "damaged",
            f"qty_{self.pants_line.pk}": "0", f"reason_{self.pants_line.pk}": "", "note": "torn", "resolution": "refund",
        }
        data.update(extra)
        return self.client.post(reverse("returns:request", kwargs={"order_id": order.pk}), data)

    def test_order_page_offers_return_buttons_only_while_eligible(self):
        order = self.make_order()
        page = self.client.get(reverse("orders:detail", kwargs={"order_id": order.pk})).content.decode()
        self.assertIn("Return this item", page)
        self.assertIn("Request a return", page)
        old = self.make_order(delivered_days_ago=40)
        page = self.client.get(reverse("orders:detail", kwargs={"order_id": old.pk})).content.decode()
        self.assertNotIn("Return this item", page)
        self.assertNotIn("Request a return", page)

    def test_form_offers_refund_or_replacement(self):
        order = self.make_order()
        page = self.client.get(reverse("returns:request", kwargs={"order_id": order.pk})).content.decode()
        self.assertIn('value="refund"', page)
        self.assertIn('value="replacement"', page)

    def test_submitting_the_form_creates_the_request_with_the_chosen_resolution(self):
        order = self.make_order()
        response = self._post(order, resolution="replacement")
        created = ReturnRequest.objects.get()
        self.assertRedirects(response, reverse("returns:detail", kwargs={"pk": created.pk}))
        self.assertEqual((created.items.count(), created.resolution), (1, "replacement"))

    def test_form_errors_are_shown_and_nothing_is_created(self):
        order = self.make_order()
        response = self._post(order, **{f"reason_{self.hoodie_line.pk}": ""})
        self.assertContains(response, "Choose a reason")
        self.assertFalse(ReturnRequest.objects.exists())

    def test_closed_window_shows_the_message_instead_of_the_form(self):
        order = self.make_order(delivered_days_ago=40)
        page = self.client.get(reverse("returns:request", kwargs={"order_id": order.pk})).content.decode()
        self.assertIn("return window", page)
        self.assertNotIn("Submit return request", page)

    def test_a_customer_cannot_see_or_return_someone_elses_order(self):
        other = ensure_customer_profile_for_user(user=User.objects.create_user("other", "o@example.com", "pw12345678"))
        order = self.make_order(profile=other)
        self.assertEqual(self.client.get(reverse("returns:request", kwargs={"order_id": order.pk})).status_code, 404)
        request = services.create_return_request(order=order, customer_profile=other, selections=[(self.hoodie_line.pk, 1, "damaged")])
        self.assertEqual(self.client.get(reverse("returns:detail", kwargs={"pk": request.pk})).status_code, 404)
        self.client.post(reverse("returns:cancel", kwargs={"pk": request.pk}))
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnStatus.REQUESTED)

    def test_login_is_required(self):
        self.client.logout()
        order = self.make_order()
        self.assertEqual(self.client.get(reverse("returns:request", kwargs={"order_id": order.pk})).status_code, 302)
        self.assertEqual(self.client.get(reverse("returns:list")).status_code, 302)

    def test_detail_wording_follows_the_stage_and_courier(self):
        india_post = services.approve_return(self.request_return(self.make_order(partner="indiapost")))
        delhivery = services.approve_return(self.request_return(self.make_order(partner="delhivery")))
        self.assertContains(self.client.get(reverse("returns:detail", kwargs={"pk": india_post.pk})), "We will arrange a pickup")
        self.assertContains(self.client.get(reverse("returns:detail", kwargs={"pk": delhivery.pk})), "Our team will contact you")

    def test_detail_shows_pickup_tracking_only_once_booked(self):
        request = self.approved_india_post()
        services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        request.refresh_from_db()
        self.assertContains(self.client.get(reverse("returns:detail", kwargs={"pk": request.pk})), request.reverse_tracking_number)

    def test_a_reserved_barcode_is_not_shown_before_the_pickup_is_booked(self):
        request = self.approved_india_post()
        self.net.book_queue.append((503, {}))
        request = services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertTrue(request.reverse_tracking_number)  # reserved...
        self.assertNotContains(self.client.get(reverse("returns:detail", kwargs={"pk": request.pk})), request.reverse_tracking_number)

    def test_list_and_detail_render(self):
        request = self.request_return(self.make_order())
        self.assertContains(self.client.get(reverse("returns:list")), request.return_number)
        self.assertContains(self.client.get(reverse("returns:detail", kwargs={"pk": request.pk})), "Cancel request")


# ============================================================================ approval

class ApprovalTests(ReturnsTestCase):
    def test_approving_only_agrees_it_nothing_is_booked(self):
        request = services.approve_return(self.request_return(self.make_order(partner="indiapost")))
        self.assertEqual(request.status, ReturnStatus.APPROVED)
        self.assertIsNotNone(request.approved_at)
        self.assertEqual(self.net.calls, [])
        self.assertEqual(request.reverse_tracking_number, "")

    def test_approval_email_depends_on_the_courier(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_return(self.request_return(self.make_order(partner="indiapost")))
        self.assertIn("arrange a pickup", mail.outbox[-1].body)
        with self.captureOnCommitCallbacks(execute=True):
            services.approve_return(self.request_return(self.make_order(partner="delhivery")))
        self.assertIn("contact you", mail.outbox[-1].body)

    def test_approve_twice_is_refused(self):
        request = services.approve_return(self.request_return(self.make_order()))
        with self.assertRaises(ReturnError):
            services.approve_return(request)

    def test_rejection_needs_a_reason_and_emails_the_customer(self):
        request = self.request_return(self.make_order())
        with self.assertRaises(ReturnError):
            services.reject_return(request, reason="  ")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.reject_return(request, reason="Item was used")
        self.assertEqual(request.status, ReturnStatus.REJECTED)
        self.assertIn("Item was used", mail.outbox[0].body)

    def test_an_approved_request_can_still_be_rejected_before_pickup(self):
        request = services.approve_return(self.request_return(self.make_order()))
        self.assertEqual(services.reject_return(request, reason="Changed our mind").status, ReturnStatus.REJECTED)


# ============================================================================ pickup (India Post only)

class PickupTests(ReturnsTestCase):
    def test_initiating_books_an_india_post_pickup_with_a_reserved_barcode(self):
        request = self.approved_india_post()
        with self.captureOnCommitCallbacks(execute=True):
            request = services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(request.status, ReturnStatus.PICKUP_SCHEDULED)
        self.assertEqual(len(request.reverse_tracking_number), 13)
        article = self.net.booking_calls()[0]["json"]["articles"][0]
        self.assertEqual((article["pickup_or_dropoff"], article["pickup_address_flag"]), ("PICKUP", "TRUE"))
        self.assertEqual(article["barcode_no"], request.reverse_tracking_number)
        self.assertEqual(article["bulk_reference"], request.return_number)
        self.assertEqual(article["sender_pincode"], "682001")
        self.assertEqual(article["pickup_schedule_date"], f"{_day(1):%m/%d/%Y} 10:00:00 AM")
        self.assertIn("Tracking number", mail.outbox[-1].body)

    def test_delhivery_orders_have_no_pickup_option(self):
        request = services.approve_return(self.request_return(self.make_order(partner="delhivery")))
        self.assertFalse(request.pickup_is_app_managed)
        with self.assertRaises(ReturnError) as ctx:
            services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertIn("India Post", str(ctx.exception))
        self.assertEqual(self.net.calls, [])
        with self.assertRaises(ReturnError):
            services.enter_pickup_manually(request, tracking_number="X1")

    def test_pickup_requires_an_approved_return(self):
        request = self.request_return(self.make_order(partner="indiapost"))
        with self.assertRaises(ReturnError):
            services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")

    def test_only_the_returned_items_weigh_into_the_parcel(self):
        request = self.approved_india_post(hoodie_qty=1)
        services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(self.net.booking_calls()[0]["json"]["articles"][0]["physical_weight"], 400)

    def test_failed_booking_keeps_the_approval_and_reuses_the_barcode_on_retry(self):
        request = self.approved_india_post()
        self.net.book_queue.append((503, {"message": "down"}))
        request = services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(request.status, ReturnStatus.APPROVED)
        self.assertIn("unavailable", request.pickup_error)
        reserved = request.reverse_tracking_number
        self.assertTrue(reserved)
        request = services.initiate_pickup(request, pickup_day=_day(2), pickup_slot="13:00-16:00")
        self.assertEqual((request.status, request.pickup_error), (ReturnStatus.PICKUP_SCHEDULED, ""))
        self.assertEqual(request.reverse_tracking_number, reserved)
        self.assertEqual(request.pickup_day, _day(2))
        self.assertEqual(IndiaPostBarcodePool.objects.get().current_number, 21433002)  # one barcode consumed

    def test_initiating_twice_sends_one_booking(self):
        request = self.approved_india_post()
        services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(len(self.net.booking_calls()), 1)

    def test_pickup_date_and_slot_are_validated(self):
        request = self.approved_india_post()
        with self.assertRaises(ReturnError):
            services.initiate_pickup(request, pickup_day=_day(-1), pickup_slot="10:00-13:00")
        with self.assertRaises(ReturnError):
            services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="midnight")
        self.assertEqual(self.net.calls, [])

    def test_missing_address_data_is_a_recorded_error_not_a_crash(self):
        order = self.make_order(partner="indiapost")
        order.delivery_address_snapshot = {"name": "R", "phone": "9876543210", "line1": "a", "city": "b", "pincode": "123"}
        order.save()
        request = services.approve_return(self.request_return(order))
        request = services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(request.status, ReturnStatus.APPROVED)
        self.assertIn("PIN", request.pickup_error)
        self.assertEqual(self.net.booking_calls(), [])

    def test_manual_tracking_number_is_the_fallback(self):
        request = self.approved_india_post()
        self.net.book_queue.append((503, {}))
        request = services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        self.assertEqual(request.status, ReturnStatus.APPROVED)
        request = services.enter_pickup_manually(request, tracking_number="EE000000001IN")
        self.assertEqual((request.status, request.reverse_tracking_number), (ReturnStatus.PICKUP_SCHEDULED, "EE000000001IN"))
        with self.assertRaises(ReturnError):
            services.enter_pickup_manually(self.approved_india_post(), tracking_number="  ")


# ============================================================================ close-out: refund or replacement

class CloseOutTests(ReturnsTestCase):
    def _received(self, resolution, partner="delhivery", **kwargs):
        request = services.approve_return(self.request_return(self.make_order(partner=partner), resolution=resolution, **kwargs))
        return services.mark_received(request, resellable=[i.pk for i in request.items.all()])

    def test_delhivery_orders_go_straight_from_approved_to_received(self):
        request = services.approve_return(self.request_return(self.make_order(partner="delhivery")))
        self.assertEqual(services.mark_received(request, resellable=[]).status, ReturnStatus.RECEIVED)

    def test_refund_path_restocks_and_records_the_amount(self):
        request = self._received("refund", hoodie_qty=2, pants_qty=1)
        self.hoodie.refresh_from_db()
        self.pants.refresh_from_db()
        self.assertEqual((self.hoodie.stock_quantity, self.pants.stock_quantity), (12, 11))
        services.mark_refunded(request)
        self.assertEqual((request.status, request.refund_amount), (ReturnStatus.REFUNDED, Decimal("1100.00")))
        self.assertIsNotNone(request.refunded_at)

    def test_refund_amount_can_be_overridden_and_cannot_be_negative(self):
        request = self._received("refund")
        with self.assertRaises(ReturnError):
            services.mark_refunded(request, amount=Decimal("-1"))
        services.mark_refunded(request, amount=Decimal("250.00"))
        self.assertEqual(request.refund_amount, Decimal("250.00"))

    def test_replacement_path_takes_the_items_out_of_stock_again(self):
        request = self._received("replacement", hoodie_qty=1, pants_qty=1)
        self.hoodie.refresh_from_db()
        self.assertEqual(self.hoodie.stock_quantity, 11)  # returned item restocked
        with self.captureOnCommitCallbacks(execute=True):
            services.mark_replaced(request, tracking_number="EE999999999IN")
        self.hoodie.refresh_from_db()
        self.pants.refresh_from_db()
        self.assertEqual((self.hoodie.stock_quantity, self.pants.stock_quantity), (10, 10))
        self.assertEqual((request.status, request.replacement_tracking), (ReturnStatus.REPLACED, "EE999999999IN"))
        self.assertIsNotNone(request.replaced_at)
        self.assertIn("EE999999999IN", mail.outbox[-1].body)

    def test_replacement_is_all_or_nothing_when_stock_is_short(self):
        request = services.approve_return(self.request_return(self.make_order(), hoodie_qty=1, pants_qty=1, resolution="replacement"))
        services.mark_received(request, resellable=[])  # damaged: nothing goes back
        Product.objects.filter(pk=self.pants.pk).update(stock_quantity=0)
        before = Product.objects.get(pk=self.hoodie.pk).stock_quantity
        with self.assertRaises(ReturnError) as ctx:
            services.mark_replaced(request)
        self.assertIn("Not enough stock", str(ctx.exception))
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnStatus.RECEIVED)
        self.assertEqual(Product.objects.get(pk=self.hoodie.pk).stock_quantity, before)  # hoodie line rolled back too
        Product.objects.filter(pk=self.pants.pk).update(stock_quantity=5)
        self.assertEqual(services.mark_replaced(request).status, ReturnStatus.REPLACED)

    def test_variant_stock_is_used_when_the_line_has_a_variant(self):
        variant = ProductVariant.objects.create(product=self.hoodie, name="M", sku_suffix="M", base_price="500", mrp="500", stock_quantity=5)
        order = self.make_order()
        self.hoodie_line.variant = variant
        self.hoodie_line.save()
        request = services.approve_return(self.request_return(order, hoodie_qty=1, resolution="replacement"))
        services.mark_received(request, resellable=[request.items.get().pk])
        variant.refresh_from_db()
        self.assertEqual(variant.stock_quantity, 6)
        services.mark_replaced(request)
        variant.refresh_from_db()
        self.assertEqual(variant.stock_quantity, 5)

    def test_refund_and_replacement_cannot_be_mixed_up(self):
        with self.assertRaises(ReturnError):
            services.mark_replaced(self._received("refund"))
        with self.assertRaises(ReturnError):
            services.mark_refunded(self._received("replacement"))

    def test_damaged_lines_are_not_restocked(self):
        request = services.approve_return(self.request_return(self.make_order(), hoodie_qty=1, pants_qty=1))
        pants_item = request.items.get(order_item=self.pants_line)
        services.mark_received(request, resellable=[request.items.get(order_item=self.hoodie_line).pk])
        self.hoodie.refresh_from_db()
        self.pants.refresh_from_db()
        self.assertEqual((self.hoodie.stock_quantity, self.pants.stock_quantity), (11, 10))
        pants_item.refresh_from_db()
        self.assertFalse(pants_item.restock)

    def test_steps_cannot_be_skipped_or_repeated(self):
        request = self.request_return(self.make_order(partner="indiapost"))
        for action in (services.mark_picked_up, lambda r: services.mark_received(r, resellable=[]), services.mark_refunded):
            with self.assertRaises(ReturnError):
                action(request)
        request = services.approve_return(request)
        services.mark_received(request, resellable=[])
        with self.assertRaises(ReturnError):
            services.mark_received(request, resellable=[])
        with self.assertRaises(ReturnError):
            services.reject_return(request, reason="late")

    def test_returns_never_change_the_order_status(self):
        order = self.make_order()
        request = services.approve_return(self.request_return(order))
        services.mark_received(request, resellable=[i.pk for i in request.items.all()])
        services.mark_refunded(request)
        order.refresh_from_db()
        self.assertEqual(order.order_status, OrderStatus.DELIVERED)

    def test_full_india_post_lifecycle(self):
        request = self.approved_india_post(hoodie_qty=2, pants_qty=1)
        services.initiate_pickup(request, pickup_day=_day(1), pickup_slot="10:00-13:00")
        services.mark_picked_up(request)
        services.mark_received(request, resellable=[i.pk for i in request.items.all()])
        services.mark_refunded(request)
        self.assertEqual(request.status, ReturnStatus.REFUNDED)


# ============================================================================ dashboard

class DashboardTests(ReturnsTestCase):
    def setUp(self):
        super().setUp()
        self.staff = User.objects.create_superuser("adm", "a@example.com", "pw12345678")
        self.client.force_login(self.staff)

    def _post(self, request, action, **data):
        return self.client.post(reverse("dashboard:return-action", kwargs={"pk": request.pk, "action": action}), data, follow=True)

    def _detail(self, request):
        return self.client.get(reverse("dashboard:return-detail", kwargs={"pk": request.pk})).content.decode()

    def test_staff_only(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("dashboard:return-list")).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("dashboard:return-list")).status_code, 403)

    def test_new_request_appears_in_the_returns_section_with_a_pending_badge(self):
        request = self.request_return(self.make_order(), resolution="replacement")
        listing = self.client.get(reverse("dashboard:return-list")).content.decode()
        self.assertIn(request.return_number, listing)
        self.assertIn("Replacement", listing)
        self.assertIn("1 awaiting review", listing)
        home = self.client.get(reverse("dashboard:home")).content.decode()
        self.assertIn(reverse("dashboard:return-list"), home)
        self.assertIn('badge bg-warning-subtle text-warning ms-auto nav-text">1<', home)

    def test_list_filter_and_search(self):
        request = self.request_return(self.make_order())
        self.assertNotContains(self.client.get(reverse("dashboard:return-list"), {"status": "refunded"}), request.return_number)
        self.assertContains(self.client.get(reverse("dashboard:return-list"), {"q": request.return_number}), request.return_number)

    def test_the_dashboard_order_page_lists_its_returns(self):
        order = self.make_order()
        request = self.request_return(order)
        page = self.client.get(reverse("dashboard:order-detail", kwargs={"pk": order.pk})).content.decode()
        self.assertIn(request.return_number, page)
        self.assertIn(reverse("dashboard:return-detail", kwargs={"pk": request.pk}), page)

    def test_a_requested_return_shows_approve_and_reject_but_no_pickup(self):
        page = self._detail(self.request_return(self.make_order(partner="indiapost")))
        self.assertIn("Approve", page)
        self.assertIn("Reject", page)
        self.assertNotIn("Initiate", page)

    def test_approving_then_initiating_the_pickup_for_an_india_post_order(self):
        request = self.request_return(self.make_order(partner="indiapost"))
        self.assertContains(self._post(request, "approve"), "Initiate pickup")
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnStatus.APPROVED)
        self.assertEqual(self.net.calls, [])
        self.assertIn("Initiate India Post pickup", self._detail(request))
        response = self._post(request, "initiate-pickup", pickup_day=_day(1).isoformat(), pickup_slot="10:00-13:00")
        self.assertContains(response, "Pickup initiated")
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnStatus.PICKUP_SCHEDULED)

    def test_delhivery_order_shows_no_initiate_pickup_option(self):
        request = self.request_return(self.make_order(partner="delhivery"))
        self.assertContains(self._post(request, "approve"), "Mark it received")
        detail = self._detail(request)
        self.assertNotIn("Initiate India Post pickup", detail)
        self.assertNotIn("Save pickup details", detail)
        self.assertIn("no pickup to initiate", detail)
        self.assertIn("Mark received", detail)
        response = self._post(request, "initiate-pickup", pickup_day=_day(1).isoformat(), pickup_slot="10:00-13:00")
        self.assertContains(response, "only booked from here for orders delivered by India Post")
        self.assertEqual(self.net.calls, [])

    def test_rejecting_from_the_dashboard(self):
        request = self.request_return(self.make_order())
        self.assertContains(self._post(request, "reject", reason=""), "reason")
        self.assertContains(self._post(request, "reject", reason="Used item"), "Return rejected")
        request.refresh_from_db()
        self.assertEqual((request.status, request.admin_note), (ReturnStatus.REJECTED, "Used item"))

    def test_booking_failure_is_shown_and_can_be_retried(self):
        request = self.approved_india_post()
        self.net.book_queue.append((503, {}))
        response = self._post(request, "initiate-pickup", pickup_day=_day(1).isoformat(), pickup_slot="10:00-13:00")
        self.assertContains(response, "could not be booked")
        self.assertContains(response, "Retry India Post pickup")
        response = self._post(request, "initiate-pickup", pickup_day=_day(1).isoformat(), pickup_slot="10:00-13:00")
        self.assertContains(response, "Pickup initiated")

    def test_refund_form_for_refund_requests_and_replacement_form_for_replacements(self):
        refund = services.approve_return(self.request_return(self.make_order(), resolution="refund"))
        services.mark_received(refund, resellable=[])
        page = self._detail(refund)
        self.assertIn("Record refund", page)
        self.assertNotIn("Record replacement sent", page)
        replacement = services.approve_return(self.request_return(self.make_order(), resolution="replacement"))
        services.mark_received(replacement, resellable=[])
        page = self._detail(replacement)
        self.assertIn("Record replacement sent", page)
        self.assertNotIn("Record refund", page)

    def test_full_replacement_flow_through_the_dashboard(self):
        request = self.request_return(self.make_order(partner="delhivery"), hoodie_qty=1, resolution="replacement")
        self._post(request, "approve")
        self._post(request, "received", resellable=[str(request.items.get().pk)])
        response = self._post(request, "replaced", tracking_number="DL555")
        self.assertContains(response, "Replacement recorded")
        request.refresh_from_db()
        self.assertEqual((request.status, request.replacement_tracking), (ReturnStatus.REPLACED, "DL555"))

    def test_full_refund_flow_through_the_dashboard(self):
        request = self.request_return(self.make_order(partner="indiapost"), hoodie_qty=1)
        self._post(request, "approve")
        self._post(request, "initiate-pickup", pickup_day=_day(1).isoformat(), pickup_slot="10:00-13:00")
        self._post(request, "picked-up")
        self._post(request, "received", resellable=[str(request.items.get().pk)])
        self._post(request, "refunded", amount="500")
        request.refresh_from_db()
        self.assertEqual((request.status, request.refund_amount), (ReturnStatus.REFUNDED, Decimal("500.00")))

    def test_stock_shortfall_on_replacement_is_reported(self):
        request = services.approve_return(self.request_return(self.make_order(), hoodie_qty=1, resolution="replacement"))
        services.mark_received(request, resellable=[])
        Product.objects.filter(pk=self.hoodie.pk).update(stock_quantity=0)
        self.assertContains(self._post(request, "replaced"), "Not enough stock")
        request.refresh_from_db()
        self.assertEqual(request.status, ReturnStatus.RECEIVED)

    def test_bad_input_and_unknown_actions(self):
        request = self.approved_india_post()
        self.assertContains(self._post(request, "initiate-pickup", pickup_day="not-a-date", pickup_slot="10:00-13:00"), "valid pickup date")
        self.assertEqual(self._post(request, "explode").status_code, 404)
        self.assertEqual(self.client.get(reverse("dashboard:return-action", kwargs={"pk": request.pk, "action": "approve"})).status_code, 405)


# ============================================================================ settings

class ReturnWindowSettingTests(ReturnsTestCase):
    def _form(self, **extra):
        from core.services import get_site_settings
        from dashboard.forms import SiteSettingsForm

        data = {"site_name": "Yarn Guy", "primary_color": "#0369A1", "secondary_color": "#0B1220",
                "font_family": "Inter", "tax_rate_percent": "0", "cod_delivery_charge": "50", **extra}
        return SiteSettingsForm(data, instance=get_site_settings())

    def test_window_can_be_changed_and_applies_immediately(self):
        form = self._form(return_window_days="14")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(SiteSettings.objects.get().return_window_days, 14)
        self.assertTrue(services.get_eligibility(self.make_order(delivered_days_ago=10)).allowed)

    def test_blank_keeps_the_saved_window_and_zero_is_allowed(self):
        SiteSettings.objects.update(return_window_days=12)
        form = self._form()
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(SiteSettings.objects.get().return_window_days, 12)
        form = self._form(return_window_days="0")
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(SiteSettings.objects.get().return_window_days, 0)

    def test_unreasonable_values_are_rejected(self):
        self.assertFalse(self._form(return_window_days="366").is_valid())
        self.assertFalse(self._form(return_window_days="-1").is_valid())
        self.assertFalse(self._form(return_window_days="abc").is_valid())
