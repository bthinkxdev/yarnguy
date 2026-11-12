"""Write operations and business rules for the catalog app."""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Union

from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from accounts.models import CustomerProfile
from catalog.exceptions import InsufficientStockError
from catalog.models import (
    ModerationStatus,
    Product,
    ProductVariant,
    ReservationStatus,
    Review,
    StockReservation,
)

if TYPE_CHECKING:
    from orders.models import Order


@transaction.atomic
def adjust_stock(
    *,
    target: Union[Product, ProductVariant],
    delta: int,
    reason: str,
) -> Union[Product, ProductVariant]:
    """
    Atomically adjust stock using select_for_update to prevent race conditions.

    Params:
        target: Product or ProductVariant instance to adjust.
        delta: Positive or negative integer change.
        reason: Audit reason string (logged in Phase 4 inventory module).
    Returns:
        Updated target instance.
    Raises:
        InsufficientStockError: When adjustment would make stock negative.
    """
    if isinstance(target, Product):
        locked = Product.objects.select_for_update().get(pk=target.pk)
        new_quantity = locked.stock_quantity + delta
        if new_quantity < 0:
            raise InsufficientStockError(f"Insufficient stock for product {locked.sku}: {reason}")
        locked.stock_quantity = new_quantity
        locked.save(update_fields=["stock_quantity", "updated_at"])
        return locked

    locked = ProductVariant.objects.select_for_update().get(pk=target.pk)
    new_quantity = locked.stock_quantity + delta
    if new_quantity < 0:
        raise InsufficientStockError(f"Insufficient stock for variant {locked}: {reason}")
    locked.stock_quantity = new_quantity
    locked.save(update_fields=["stock_quantity", "updated_at"])
    return locked


@transaction.atomic
def submit_review(
    *,
    product: Product,
    customer: CustomerProfile,
    rating: int,
    title: str,
    body: str,
    is_verified_purchase: bool = False,
) -> Review:
    """
    Create a review in PENDING moderation state and notify admins via Celery.

    Params:
        product: Reviewed product.
        customer: Author customer profile.
        rating: Star rating 1–5.
        title: Review headline.
        body: Review body text.
        is_verified_purchase: Whether the customer bought this product.
    Returns:
        Created Review in PENDING status.
    """
    from catalog.tasks import notify_review_moderation

    review = Review.objects.create(
        product=product,
        customer=customer,
        rating=rating,
        title=title,
        body=body,
        is_verified_purchase=is_verified_purchase,
        moderation_status=ModerationStatus.PENDING,
    )
    notify_review_moderation.delay(review_id=review.pk)
    return review


@transaction.atomic
def moderate_review(
    *,
    review_id: int,
    decision: str,
    moderator: User,
) -> Review:
    """
    Transition a review's moderation_status to APPROVED or REJECTED.

    Params:
        review_id: Primary key of the review.
        decision: ModerationStatus value (approved or rejected).
        moderator: Admin user performing moderation.
    Returns:
        Updated Review instance.
    """
    if decision not in (ModerationStatus.APPROVED, ModerationStatus.REJECTED):
        raise ValueError(f"Invalid moderation decision: {decision}")

    review = Review.objects.select_for_update().get(pk=review_id)
    review.moderation_status = decision
    review.moderated_by = moderator
    review.save(update_fields=["moderation_status", "moderated_by", "updated_at"])
    
    #dismiss the notification for all admins
    from notifications.models import Notification
    body_text = f'Review "{review.title}" on {review.product.name} awaits approval.'
    Notification.objects.filter(
        title="Review pending moderation", 
        body=body_text,
        is_read=False
    ).update(is_read=True)

    return review


def get_available_stock(*, target: Union[Product, ProductVariant]) -> int:
    """Calculates actual physical stock minus active unexpired reservations."""
    now = timezone.now()
    StockReservation.objects.filter(
        status=ReservationStatus.RESERVED,
        expires_at__lte=now,
    ).update(status=ReservationStatus.RELEASED, updated_at=now)

    if isinstance(target, Product):
        reserved = StockReservation.objects.filter(
            product=target,
            variant__isnull=True,
            status=ReservationStatus.RESERVED,
            expires_at__gt=now,
        ).aggregate(total=Sum("quantity"))["total"] or 0
        return max(0, target.stock_quantity - reserved)

    reserved = StockReservation.objects.filter(
        variant=target,
        status=ReservationStatus.RESERVED,
        expires_at__gt=now,
    ).aggregate(total=Sum("quantity"))["total"] or 0
    return max(0, target.stock_quantity - reserved)


@transaction.atomic
def reserve_inventory_for_order(*, order: Order, minutes: int = 15) -> bool:
    """Creates a 15-minute temporary reservation with row locks."""
    expires_at = timezone.now() + timedelta(minutes=minutes)

    StockReservation.objects.filter(order=order, status=ReservationStatus.RESERVED).update(
        status=ReservationStatus.RELEASED
    )

    for item in order.items.all():
        if item.variant:
            locked = ProductVariant.objects.select_for_update().get(pk=item.variant.pk)
        else:
            locked = Product.objects.select_for_update().get(pk=item.product.pk)

        if get_available_stock(target=locked) < item.quantity:
            return False

    for item in order.items.all():
        StockReservation.objects.create(
            order=order,
            product=item.product,
            variant=item.variant,
            quantity=item.quantity,
            status=ReservationStatus.RESERVED,
            expires_at=expires_at,
        )
    return True


@transaction.atomic
def commit_inventory_reservations(*, order: Order) -> None:
    """Marks reservation as COMMITTED (orders.services handles the physical deduction)."""
    StockReservation.objects.select_for_update().filter(
        order=order, status=ReservationStatus.RESERVED
    ).update(status=ReservationStatus.COMMITTED, updated_at=timezone.now())


@transaction.atomic
def release_inventory_reservations(*, order: Order) -> None:
    """Releases reservation hold immediately."""
    StockReservation.objects.filter(
        order=order, status=ReservationStatus.RESERVED
    ).update(status=ReservationStatus.RELEASED, updated_at=timezone.now())

