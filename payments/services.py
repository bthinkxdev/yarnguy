"""Write operations and business rules for the payments app."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction

from payments.models import PaymentStatus, PaymentTransaction
from payments.registry import get_payment_adapter

if TYPE_CHECKING:
    from orders.models import Order


@transaction.atomic
def confirm_payment_success(*, payment_transaction: PaymentTransaction) -> PaymentTransaction:
    """
    Single convergence point for successful payment — sync or async.

    Order confirmation logic lives here exclusively regardless of gateway.
    """
    payment_transaction.status = PaymentStatus.SUCCESS
    payment_transaction.save(update_fields=["status", "updated_at"])

    #empty and deactivate the cart now that payment is confirmed
    order = payment_transaction.order
    if order:
        from checkout.models import CheckoutSession, CheckoutSessionStatus
        session = CheckoutSession.objects.filter(order=order).first()
        if session:
            session.status = CheckoutSessionStatus.COMPLETED
            session.save(update_fields=["status", "updated_at"])

        if order.cart:
            cart = order.cart
            from cart.models import CartItem
            CartItem.objects.filter(cart=cart).delete()

        #send order placement confirmation email
        from notifications.tasks import dispatch_order_confirmation_notification
        transaction.on_commit(
            lambda: dispatch_order_confirmation_notification.delay(order_id=order.pk)
        )

    return payment_transaction


@transaction.atomic
def confirm_payment_failed(*, payment_transaction: PaymentTransaction) -> PaymentTransaction:
    """Mark a payment transaction as failed."""
    payment_transaction.status = PaymentStatus.FAILED
    payment_transaction.save(update_fields=["status", "updated_at"])
    return payment_transaction


@transaction.atomic
def process_payment(
    *,
    order: Order,
    gateway_key: str,
    payment_data: dict[str, Any],
) -> PaymentTransaction:
    """
    Process payment via registry-selected adapter.

    Writes PaymentTransaction as PENDING immediately. Sync gateways capture
    inline; async gateways complete via webhook → confirm_payment_success.
    """
    adapter = get_payment_adapter(gateway_key=gateway_key)
    metadata = {"order_id": order.pk, "order_number": order.order_number, **payment_data}

    #clean up: mark any previous abandoned payment attempts as failed
    PaymentTransaction.objects.filter(
        order=order,
        status=PaymentStatus.PENDING
    ).update(status=PaymentStatus.FAILED)

    payment_tx = PaymentTransaction.objects.create(
        order=order,
        gateway_key=gateway_key,
        amount=order.total_amount,
        currency=order.currency,
        status=PaymentStatus.PENDING,
        metadata=metadata,
    )

    intent = adapter.create_payment_intent(
        amount=order.total_amount,
        currency=order.currency.code,
        metadata=metadata,
    )
    payment_tx.external_intent_id = intent.intent_id
    payment_tx.save(update_fields=["external_intent_id", "updated_at"])

    if adapter.is_async:
        return payment_tx

    capture = adapter.capture(intent_id=intent.intent_id)

    payment_tx.external_transaction_id = capture.transaction_id
    payment_tx.metadata.update(capture.metadata)
    payment_tx.save(update_fields=["external_transaction_id", "metadata", "updated_at"])

    if capture.success:
        confirm_payment_success(payment_transaction=payment_tx)
        #COD checkouts are successful, but the payment itself should remain Pending until the admin manually collects the cash and marks it as Success.
        if gateway_key == "cod":
            payment_tx.status = PaymentStatus.PENDING
            payment_tx.save(update_fields=["status", "updated_at"])
    else:
        confirm_payment_failed(payment_transaction=payment_tx)

    return payment_tx


@transaction.atomic
def handle_payment_webhook(
    *,
    gateway_key: str,
    payload: bytes,
    signature: str,
) -> PaymentTransaction | None:
    """
    Verify webhook via adapter and converge on confirm_payment_success/failed.
    """
    adapter = get_payment_adapter(gateway_key=gateway_key)
    event = adapter.verify_webhook(payload=payload, signature=signature)

    intent_id = event.get("intent_id", "")
    payment_tx = PaymentTransaction.objects.filter(
        external_intent_id=intent_id,
        gateway_key=gateway_key,
    ).first()
    if payment_tx is None:
        return None

    if event.get("status") == "success":
        payment_tx.external_transaction_id = event.get("transaction_id", "")
        payment_tx.save(update_fields=["external_transaction_id", "updated_at"])
        return confirm_payment_success(payment_transaction=payment_tx)

    return confirm_payment_failed(payment_transaction=payment_tx)
