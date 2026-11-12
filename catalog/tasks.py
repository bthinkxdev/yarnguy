"""Celery tasks for the catalog app."""

from __future__ import annotations

from celery import shared_task
from django.contrib.auth import get_user_model
from django.utils import timezone

from catalog.models import ReservationStatus, Review, StockReservation
from notifications.services import create_notification

User = get_user_model()


@shared_task(name="release_expired_stock_reservations")
def release_expired_stock_reservations():
    """Runs every 1 minute to release reservations older than 15 minutes."""
    now = timezone.now()
    count = StockReservation.objects.filter(
        status=ReservationStatus.RESERVED,
        expires_at__lte=now,
    ).update(status=ReservationStatus.RELEASED, updated_at=now)
    return f"Released {count} expired reservations."


@shared_task(name="catalog.tasks.notify_review_moderation")
def notify_review_moderation(*, review_id: int) -> None:
    """Notify SuperAdmin users that a review awaits moderation."""
    review = Review.objects.select_related("product").get(pk=review_id)
    admin_users = User.objects.filter(groups__name="SuperAdmin").distinct()
    for admin_user in admin_users:
        create_notification(
            user=admin_user,
            title="Review pending moderation",
            body=f'Review "{review.title}" on {review.product.name} awaits approval.',
        )
