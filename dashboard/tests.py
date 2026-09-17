"""Tests for the dashboard app.

Access control, CRUD flows, and report views are exercised here. Add cases
under a tests/ package as coverage grows (see scripts/scaffold_apps.py).
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from accounts.services import register_customer_email
from cart.models import Cart, CartItem
from catalog.models import Category, Product
from checkout.services import create_checkout_session, place_order
from core.models import Currency
from dashboard.forms import ProductVariantForm, ProductVariantFormSet
from orders.models import Order, OrderStatus
from payments.models import PaymentStatus, PaymentTransaction
from payments.services import confirm_payment_success


class OrderListTabsTests(TestCase):
    """The Orders list splits real orders from CHECKOUT_PENDING abandoned
    checkouts into two tabs (see dashboard/views/orders.py::order_list)."""

    def setUp(self) -> None:
        self.staff_user = User.objects.create_superuser(
            username="dash-admin", email="dash-admin@example.com", password="testpass12345"
        )
        self.client.force_login(self.staff_user)

        self.currency, _ = Currency.objects.get_or_create(
            code="INR",
            defaults={"symbol": "₹", "exchange_rate_to_base": "1.00000000", "is_default": True},
        )
        self.profile = register_customer_email(
            email="dash-order-test@example.com", password="testpass12345", name="Dash Order Test"
        )
        category = Category.objects.create(name="Test Category", slug="dash-test-category")
        self.product = Product.objects.create(
            name="Test Yarn",
            slug="dash-test-yarn",
            sku="SKU-DASH-1",
            category=category,
            base_price="500.00",
            mrp="500.00",
            purchase_price="300.00",
            stock_quantity=100,
        )

        self.confirmed_order = self._place_order("confirmed")
        tx = PaymentTransaction.objects.create(
            order=self.confirmed_order,
            gateway_key="razorpay_upi",
            amount=self.confirmed_order.total_amount,
            currency=self.confirmed_order.currency,
            status=PaymentStatus.PENDING,
            external_intent_id="order_dash_confirmed_1",
        )
        confirm_payment_success(payment_transaction=tx, external_transaction_id="pay_dash_confirmed_1")

        self.abandoned_order = self._place_order("abandoned")
        self.assertEqual(self.abandoned_order.order_status, OrderStatus.CHECKOUT_PENDING)

    def _place_order(self, suffix: str):
        cart = Cart.objects.create(customer_profile=self.profile, currency=self.currency)
        CartItem.objects.create(
            cart=cart, product=self.product, quantity=1, unit_price_at_add=self.product.base_price
        )
        session = create_checkout_session(cart=cart, customer_profile=self.profile)
        return place_order(
            checkout_session_id=session.pk,
            idempotency_key=f"dash-order-test-{suffix}",
            gateway_key="razorpay_upi",
            customer_profile=self.profile,
        )

    def test_orders_tab_excludes_abandoned_checkouts(self):
        response = self.client.get(reverse("dashboard:order-list"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        self.assertContains(response, self.confirmed_order.order_number)
        self.assertNotContains(response, self.abandoned_order.order_number)
        self.assertIn("Abandoned Checkouts", content)
        self.assertIn(">1<", content)  # abandoned_count badge
        self.assertNotIn("Payment Pending", content)  # excluded from status dropdown + rows

    def test_abandoned_tab_shows_only_abandoned_checkouts(self):
        response = self.client.get(reverse("dashboard:order-list") + "?view=abandoned")
        self.assertEqual(response.status_code, 200)

        self.assertContains(response, self.abandoned_order.order_number)
        self.assertNotContains(response, self.confirmed_order.order_number)
        # status dropdown is hidden entirely on this tab
        self.assertNotContains(response, "All order statuses")

    def test_search_filter_works_within_abandoned_tab(self):
        response = self.client.get(
            reverse("dashboard:order-list") + f"?view=abandoned&q={self.abandoned_order.order_number}"
        )
        self.assertContains(response, self.abandoned_order.order_number)

        response = self.client.get(reverse("dashboard:order-list") + "?view=abandoned&q=NO_SUCH_ORDER")
        self.assertNotContains(response, self.abandoned_order.order_number)


class OrderAddressUpdateViewTests(TestCase):
    """dashboard:order-address-update — the previously-missing address edit."""

    def setUp(self) -> None:
        self.staff_user = User.objects.create_superuser(
            username="dash-admin-addr", email="dash-admin-addr@example.com", password="testpass12345"
        )
        self.client.force_login(self.staff_user)

        self.currency, _ = Currency.objects.get_or_create(
            code="INR",
            defaults={"symbol": "₹", "exchange_rate_to_base": "1.00000000", "is_default": True},
        )
        self.profile = register_customer_email(
            email="dash-addr-test@example.com", password="testpass12345", name="Dash Addr Test"
        )
        self.editable_order = Order.objects.create(
            customer_profile=self.profile,
            order_number="#ADDR-EDITABLE",
            idempotency_key="addr-editable",
            order_status=OrderStatus.PLACED_COD,
            total_amount="500.00",
            currency=self.currency,
            delivery_address_snapshot={
                "name": "Original Name", "line1": "Old Line 1", "city": "Old City",
                "state": "Old State", "pincode": "111111",
            },
        )
        self.delivered_order = Order.objects.create(
            customer_profile=self.profile,
            order_number="#ADDR-DELIVERED",
            idempotency_key="addr-delivered",
            order_status=OrderStatus.DELIVERED,
            total_amount="500.00",
            currency=self.currency,
            delivery_address_snapshot={"name": "Frozen Name", "line1": "Frozen Line 1", "city": "Frozen City"},
        )

    def test_editable_status_updates_snapshot(self) -> None:
        response = self.client.post(
            reverse("dashboard:order-address-update", args=[self.editable_order.pk]),
            data={"name": "New Name", "line1": "New Line 1", "city": "New City", "state": "New State", "pincode": "222222"},
        )
        self.assertRedirects(response, reverse("dashboard:order-detail", args=[self.editable_order.pk]))
        self.editable_order.refresh_from_db()
        self.assertEqual(self.editable_order.delivery_address_snapshot["line1"], "New Line 1")
        self.assertEqual(self.editable_order.delivery_address_snapshot["city"], "New City")

    def test_blocked_status_rejects_update(self) -> None:
        response = self.client.post(
            reverse("dashboard:order-address-update", args=[self.delivered_order.pk]),
            data={"name": "New Name", "line1": "New Line 1", "city": "New City"},
        )
        self.assertRedirects(response, reverse("dashboard:order-detail", args=[self.delivered_order.pk]))
        self.delivered_order.refresh_from_db()
        self.assertEqual(self.delivered_order.delivery_address_snapshot["line1"], "Frozen Line 1")


class ProductVariantHasChangedTests(TestCase):
    """
    A variant row an admin explicitly filled with legitimate-but-default-
    looking values (stock 0, threshold 5) must not be silently dropped as an
    "untouched extra" row.
    """

    def test_explicit_zero_values_count_as_changed(self) -> None:
        data = {
            "variants-0-variant_type": "",
            "variants-0-name": "",
            "variants-0-base_price": "0",
            "variants-0-mrp": "0",
            "variants-0-purchase_price": "0",
            "variants-0-sku_suffix": "",
            "variants-0-stock_quantity": "0",
            "variants-0-low_stock_threshold": "5",
        }
        form = ProductVariantForm(data, prefix="variants-0")
        self.assertTrue(form.has_changed())

    def test_fully_blank_row_is_unchanged(self) -> None:
        data = {
            "variants-0-variant_type": "",
            "variants-0-name": "",
            "variants-0-base_price": "",
            "variants-0-mrp": "",
            "variants-0-purchase_price": "",
            "variants-0-sku_suffix": "",
            "variants-0-stock_quantity": "",
            "variants-0-low_stock_threshold": "",
        }
        form = ProductVariantForm(data, prefix="variants-0")
        self.assertFalse(form.has_changed())


class ProductVariantFormSetValidationTests(TestCase):
    """Cross-row validation added to ProductVariantInlineFormSet."""

    def setUp(self) -> None:
        category = Category.objects.create(name="Formset Category", slug="formset-category")
        self.product = Product.objects.create(
            name="Formset Yarn",
            slug="formset-yarn",
            sku="SKU-FORMSET-1",
            category=category,
            base_price="100.00",
            mrp="100.00",
            purchase_price="60.00",
        )

    def _management_form(self, total: int) -> dict:
        return {
            "variants-TOTAL_FORMS": str(total),
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
        }

    def _row(self, index: int, **overrides) -> dict:
        row = {
            f"variants-{index}-variant_type": "Size",
            f"variants-{index}-name": f"Variant {index}",
            f"variants-{index}-base_price": "90.00",
            f"variants-{index}-mrp": "90.00",
            f"variants-{index}-purchase_price": "50.00",
            f"variants-{index}-sku_suffix": f"SKU{index}",
            f"variants-{index}-stock_quantity": "5",
            f"variants-{index}-low_stock_threshold": "5",
        }
        for key, value in overrides.items():
            row[f"variants-{index}-{key}"] = value
        return row

    def test_duplicate_sku_suffix_rejected(self) -> None:
        data = self._management_form(2)
        data.update(self._row(0, sku_suffix="Small"))
        data.update(self._row(1, sku_suffix="small"))  # case-insensitive collision
        formset = ProductVariantFormSet(data, instance=self.product, prefix="variants")
        self.assertFalse(formset.is_valid())

    def test_variant_type_casing_normalized_to_first_seen(self) -> None:
        data = self._management_form(2)
        data.update(self._row(0, variant_type="Size"))
        data.update(self._row(1, variant_type="size", sku_suffix="SKU1"))
        formset = ProductVariantFormSet(data, instance=self.product, prefix="variants")
        self.assertTrue(formset.is_valid())
        self.assertEqual(formset.forms[1].cleaned_data["variant_type"], "Size")

    def test_only_one_default_variant_allowed(self) -> None:
        data = self._management_form(2)
        data.update(self._row(0, is_default="on"))
        data.update(self._row(1, is_default="on"))
        formset = ProductVariantFormSet(data, instance=self.product, prefix="variants")
        self.assertFalse(formset.is_valid())


class ProductFormRequiredPriceGateTests(TestCase):
    """
    ProductForm.clean() silently defaults blank base_price/mrp/purchase_price/
    stock_quantity to 0 — fine when a variant will drive those fields, but a
    variant-less product must actually require them, matching the form's own
    (previously dead) error_messages.
    """

    def setUp(self) -> None:
        self.staff_user = User.objects.create_superuser(
            username="dash-admin-price", email="dash-admin-price@example.com", password="testpass12345"
        )
        self.client.force_login(self.staff_user)
        self.category = Category.objects.create(name="Price Category", slug="price-category")

    def _base_post(self, **overrides) -> dict:
        data = {
            "name": "Priced Yarn",
            "sku": "SKU-PRICE-1",
            "category": str(self.category.pk),
            "variants-TOTAL_FORMS": "0",
            "variants-INITIAL_FORMS": "0",
            "variants-MIN_NUM_FORMS": "0",
            "variants-MAX_NUM_FORMS": "1000",
            "images-TOTAL_FORMS": "0",
            "images-INITIAL_FORMS": "0",
            "images-MIN_NUM_FORMS": "0",
            "images-MAX_NUM_FORMS": "1000",
            "specifications-TOTAL_FORMS": "0",
            "specifications-INITIAL_FORMS": "0",
            "specifications-MIN_NUM_FORMS": "0",
            "specifications-MAX_NUM_FORMS": "1000",
        }
        data.update(overrides)
        return data

    def test_no_variants_and_blank_prices_is_rejected(self) -> None:
        response = self.client.post(reverse("dashboard:product-create"), data=self._base_post())
        self.assertEqual(response.status_code, 200)  # re-renders the form with errors
        self.assertFalse(Product.objects.filter(sku="SKU-PRICE-1").exists())

    def test_no_variants_with_prices_is_accepted(self) -> None:
        response = self.client.post(reverse("dashboard:product-create"), data=self._base_post(
            base_price="100.00", mrp="120.00", purchase_price="60.00", stock_quantity="10",
        ))
        self.assertRedirects(response, reverse("dashboard:product-list"))
        self.assertTrue(Product.objects.filter(sku="SKU-PRICE-1").exists())
