"""Tests for the catalog app."""

from __future__ import annotations

from django.db.models import ProtectedError
from django.test import TestCase

from accounts.services import register_customer_email
from catalog.models import Category, Product, ProductVariant
from core.models import Currency
from orders.models import Order, OrderItem, OrderStatus


class ProductDisplayVariantTests(TestCase):
    """Product.display_variant drives storefront price/stock display and (via
    dashboard.views.catalog._render_product_form) the admin's product-level
    price/stock sync — both must agree on which variant is authoritative."""

    def setUp(self) -> None:
        category = Category.objects.create(name="Variant Category", slug="variant-category")
        self.product = Product.objects.create(
            name="Variant Yarn",
            slug="variant-yarn",
            sku="SKU-VARIANT-1",
            category=category,
            base_price="100.00",
            mrp="100.00",
            purchase_price="60.00",
        )

    def test_display_variant_prefers_is_default_over_first_in_stock(self) -> None:
        default_but_out_of_stock = ProductVariant.objects.create(
            product=self.product, variant_type="Size", name="S",
            base_price="90.00", mrp="90.00", purchase_price="50.00",
            stock_quantity=0, is_default=True,
        )
        ProductVariant.objects.create(
            product=self.product, variant_type="Size", name="M",
            base_price="110.00", mrp="110.00", purchase_price="70.00",
            stock_quantity=5, is_default=False,
        )
        self.assertEqual(self.product.display_variant.pk, default_but_out_of_stock.pk)

    def test_display_variant_falls_back_to_first_in_stock_when_no_default(self) -> None:
        ProductVariant.objects.create(
            product=self.product, variant_type="Size", name="S",
            base_price="90.00", mrp="90.00", purchase_price="50.00",
            stock_quantity=0,
        )
        in_stock = ProductVariant.objects.create(
            product=self.product, variant_type="Size", name="M",
            base_price="110.00", mrp="110.00", purchase_price="70.00",
            stock_quantity=5,
        )
        self.assertEqual(self.product.display_variant.pk, in_stock.pk)


class ProductVariantDeleteProtectionTests(TestCase):
    """A variant referenced by an existing OrderItem must not be deletable —
    OrderItem.variant is PROTECT (not SET_NULL) precisely so historical order
    data can't silently go missing."""

    def setUp(self) -> None:
        category = Category.objects.create(name="Protect Category", slug="protect-category")
        self.product = Product.objects.create(
            name="Protect Yarn",
            slug="protect-yarn",
            sku="SKU-PROTECT-1",
            category=category,
            base_price="100.00",
            mrp="100.00",
            purchase_price="60.00",
        )
        self.variant = ProductVariant.objects.create(
            product=self.product, variant_type="Size", name="S",
            base_price="90.00", mrp="90.00", purchase_price="50.00",
            stock_quantity=5,
        )
        self.currency, _ = Currency.objects.get_or_create(
            code="INR",
            defaults={"symbol": "₹", "exchange_rate_to_base": "1.00000000", "is_default": True},
        )
        self.profile = register_customer_email(
            email="protect-test@example.com", password="testpass12345", name="Protect Test"
        )
        order = Order.objects.create(
            customer_profile=self.profile,
            order_number="#PROTECT-1",
            idempotency_key="protect-1",
            order_status=OrderStatus.PLACED_COD,
            subtotal="90.00",
            total_amount="90.00",
            currency=self.currency,
        )
        OrderItem.objects.create(
            order=order, product=self.product, variant=self.variant,
            variant_name=self.variant.name, variant_sku="SKU-PROTECT-1",
            quantity=1, unit_price="90.00",
        )

    def test_deleting_variant_referenced_by_order_raises_protected_error(self) -> None:
        with self.assertRaises(ProtectedError):
            self.variant.delete()
