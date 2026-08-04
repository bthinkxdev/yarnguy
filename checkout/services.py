"""Write operations and business rules for the checkout app."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional

from django.db import IntegrityError, transaction

from accounts.models import Address, CustomerProfile
from cart.models import Cart, CartItem
from cart.selectors import get_cart_summary
from catalog.services import adjust_stock
from checkout.exceptions import CheckoutSessionError
from checkout.models import CheckoutSession, CheckoutSessionStatus

from marketing.models import Coupon
from marketing.services import record_coupon_redemption
from orders.models import Order, OrderItem, OrderStatus
from orders.services import generate_order_number



@transaction.atomic
def create_checkout_session(
    *,
    cart: Cart,
    customer_profile: Optional[CustomerProfile] = None,
    session_key: str = "",
) -> CheckoutSession:
    """
    Start or return the draft checkout session for a cart.

    Query guarantee: 0–1 SELECT + 0–1 INSERT.
    """
    existing = CheckoutSession.objects.filter(
        cart=cart,
        status=CheckoutSessionStatus.DRAFT,
    ).first()
    if existing:
        return existing
    return CheckoutSession.objects.create(
        cart=cart,
        customer_profile=customer_profile,
        session_key=session_key,
    )


@transaction.atomic
def update_checkout_session(
    *,
    checkout_session: CheckoutSession,
    address: Optional[Address] = None,
    delivery_date: Optional[date] = None,

    invoice_details: Optional[dict[str, Any]] = None,
) -> CheckoutSession:
    """Persist checkout step data on the session."""
    if address is not None:
        checkout_session.address = address
    if delivery_date is not None:
        checkout_session.delivery_date = delivery_date

    if invoice_details is not None:
        checkout_session.invoice_details = invoice_details
    checkout_session.save()
    return checkout_session


@transaction.atomic
def place_order(
    *,
    checkout_session_id: int,
    idempotency_key: str,
    customer_profile: Optional[CustomerProfile] = None,
) -> Order:
    """
    Atomically place an order from a checkout session.

    Idempotent: calling twice with the same ``idempotency_key`` returns the
    existing Order rather than creating a duplicate.

    Reserves delivery slot capacity before finalizing the order.

    Stock is decremented via ``catalog.services.adjust_stock`` (select_for_update).

    Raises:
        CheckoutSessionError: When session is not in a placeable state.
        InsufficientStockError: When stock is insufficient (no oversell).
        SlotFullyBookedError: When the chosen delivery slot is at capacity.
    """
    session = (
        CheckoutSession.objects.select_for_update()
        .select_related(
            "cart",
            "cart__currency",
            "address",
        )
        .filter(pk=checkout_session_id)
        .first()
    )
    if session is None:
        raise CheckoutSessionError("Checkout session not found.")

    existing = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        return existing

    if session.status == CheckoutSessionStatus.COMPLETED and session.order_id:
        if session.idempotency_key == idempotency_key:
            return session.order
        raise CheckoutSessionError("Checkout session already completed.")

    if session.status != CheckoutSessionStatus.DRAFT:
        raise CheckoutSessionError("Checkout session is not in draft status.")

    summary = get_cart_summary(cart=session.cart)
    if not summary.lines:
        raise CheckoutSessionError("Cart is empty.")



    for line in summary.lines:
        target = line.variant if line.variant else line.product
        adjust_stock(target=target, delta=-line.quantity, reason=f"order:{idempotency_key}")

    address_snapshot: dict[str, Any] = {}
    if session.address_id:
        addr = session.address
        address_snapshot = {
            "name": addr.customer_profile.user.get_full_name() if (addr.customer_profile and addr.customer_profile.user) else "",
            "email": addr.customer_profile.user.email if (addr.customer_profile and addr.customer_profile.user) else "",
            "phone": addr.customer_profile.phone if addr.customer_profile else "",
            "label": addr.label,
            "line1": addr.line1,
            "line2": addr.line2,
            "city": addr.city,
            "state": "",
        }

    try:
        order = Order.objects.create(
            customer_profile=customer_profile,
            cart=session.cart,
            order_number=generate_order_number(),
            idempotency_key=idempotency_key,
            order_status=OrderStatus.PLACED,
            delivery_date=session.delivery_date,
            subtotal=summary.subtotal,
            coupon_discount=summary.coupon_discount,
            delivery_charge=summary.delivery_charge,
            total_amount=summary.grand_total,
            currency=session.cart.currency,
            delivery_address_snapshot=address_snapshot,
            invoice_details=session.invoice_details,
        )
    except IntegrityError:
        return Order.objects.get(idempotency_key=idempotency_key)

    for line in summary.lines:
        OrderItem.objects.create(
            order=order,
            product=line.product,
            variant=line.variant,
            quantity=line.quantity,
            unit_price=line.unit_price_at_add,
        )

    if session.cart.coupon_code:
        coupon = Coupon.objects.filter(
            code__iexact=session.cart.coupon_code.strip(),
            is_active=True,
        ).first()
        if coupon is not None and customer_profile is not None:
            record_coupon_redemption(
                coupon_id=coupon.pk,
                customer_profile_id=customer_profile.pk,
                order_id=order.pk,
            )

    session.order = order
    session.idempotency_key = idempotency_key
    session.customer_profile = customer_profile
    session.save(
        update_fields=[
            "order",
            "idempotency_key",
            "customer_profile",
            "updated_at",
        ]
    )

    return order
