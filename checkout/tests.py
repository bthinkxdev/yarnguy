"""Tests for the Razorpay browser-redirect checkout callback."""

from __future__ import annotations

from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse

from accounts.services import register_customer_email
from cart.models import Cart, CartItem
from catalog.models import Category, Product
from checkout.services import create_checkout_session, place_order
from core.models import Currency
from orders.models import OrderStatus, OrderStatusHistory
from payments.models import PaymentStatus, PaymentTransaction


class RazorpayCallbackViewTests(TestCase):
    """
    adapter.capture_payment()'s result must gate confirmation: a valid redirect
    signature only proves the payment *request* was authentic, not that the
    money was actually captured (relevant when auto-capture is off). Confirming
    regardless of the capture result would ship orders that were never paid.
    """

    def setUp(self) -> None:
        self.currency, _ = Currency.objects.get_or_create(
            code="INR",
            defaults={"symbol": "₹", "exchange_rate_to_base": "1.00000000", "is_default": True},
        )
        self.profile = register_customer_email(
            email="callback-test@example.com",
            password="testpass12345",
            name="Callback Test",
        )
        category = Category.objects.create(name="Test Category", slug="test-category")
        self.product = Product.objects.create(
            name="Test Yarn",
            slug="test-yarn",
            sku="SKU-TEST-CB-1",
            category=category,
            base_price="500.00",
            mrp="500.00",
            purchase_price="300.00",
            stock_quantity=100,
        )
        self._delhivery_patch = patch("delhivery.tasks.create_shipment_for_order.delay")
        self._delhivery_patch.start()
        self.addCleanup(self._delhivery_patch.stop)

        cart = Cart.objects.create(customer_profile=self.profile, currency=self.currency)
        CartItem.objects.create(
            cart=cart,
            product=self.product,
            quantity=1,
            unit_price_at_add=self.product.base_price,
        )
        session = create_checkout_session(cart=cart, customer_profile=self.profile)
        self.order = place_order(
            checkout_session_id=session.pk,
            idempotency_key="callback-test-order-1",
            gateway_key="razorpay_upi",
            customer_profile=self.profile,
        )
        self.tx = PaymentTransaction.objects.create(
            order=self.order,
            gateway_key="razorpay_upi",
            amount=self.order.total_amount,
            currency=self.order.currency,
            status=PaymentStatus.PENDING,
            external_intent_id="order_rzp_test_1",
        )
        self.callback_url = reverse("checkout:razorpay-callback")

    @patch("payments.adapters.concrete.RazorpayAdapter.capture_payment", return_value=True)
    @patch("payments.adapters.concrete.RazorpayAdapter.verify_payment_signature", return_value=True)
    def test_valid_signature_and_successful_capture_confirms_order(self, mock_verify, mock_capture):
        response = self.client.post(
            self.callback_url,
            data={
                "order_id": self.order.pk,
                "razorpay_payment_id": "pay_real",
                "razorpay_order_id": "order_rzp_test_1",
                "razorpay_signature": "irrelevant-signature-is-mocked",
            },
        )
        self.order.refresh_from_db()
        self.tx.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.CONFIRMED)
        self.assertEqual(self.tx.status, PaymentStatus.SUCCESS)
        self.assertEqual(self.tx.external_transaction_id, "pay_real")
        self.assertRedirects(
            response,
            reverse("checkout:confirmation", kwargs={"order_id": self.order.pk}),
            fetch_redirect_response=False,
        )

    @patch("payments.adapters.concrete.RazorpayAdapter.capture_payment", return_value=False)
    @patch("payments.adapters.concrete.RazorpayAdapter.verify_payment_signature", return_value=True)
    def test_valid_signature_but_failed_capture_does_not_confirm_order(self, mock_verify, mock_capture):
        response = self.client.post(
            self.callback_url,
            data={
                "order_id": self.order.pk,
                "razorpay_payment_id": "pay_never_captured",
                "razorpay_order_id": "order_rzp_test_1",
                "razorpay_signature": "irrelevant-signature-is-mocked",
            },
        )
        self.order.refresh_from_db()
        self.tx.refresh_from_db()
        self.assertEqual(self.order.order_status, OrderStatus.CHECKOUT_PENDING)
        self.assertEqual(self.tx.status, PaymentStatus.FAILED)
        self.assertEqual(OrderStatusHistory.objects.filter(order=self.order).count(), 0)
        self.assertRedirects(response, reverse("checkout:checkout"), fetch_redirect_response=False)
