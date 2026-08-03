"""Read-only query functions for the delivery app; views must not call the ORM directly."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional, Union

from django.db.models import F, OuterRef, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from catalog.models import Product
from delivery.models import City, Country


def get_active_countries() -> list[Country]:
    """Return all active delivery countries for the header selector. 1 SELECT."""
    return list(Country.objects.filter(is_active=True).order_by("name"))


def get_city_by_slug(*, slug: str) -> Optional[City]:
    """
    Return an active city by slug.

    Query guarantee: exactly 1 SELECT on delivery_city filtered by slug (indexed).
    """
    return City.objects.select_related("country").filter(slug=slug, is_active=True).first()


def get_active_cities() -> list[City]:
    """Return all active delivery cities. Query guarantee: 1 SELECT."""
    return list(City.objects.select_related("country").filter(is_active=True).order_by("name"))


def _same_day_allowed(*, city: City, delivery_date: date) -> bool:
    """Return True when same-day slots may still be offered for a city."""
    today = timezone.localdate()
    if delivery_date != today:
        return delivery_date > today
    return timezone.localtime().hour < city.same_day_cutoff_hour


def get_earliest_delivery_estimate(*, product: Product, destination_city: City) -> dict[str, Any]:
    """
    Return the earliest delivery estimate for a product to a destination city.

    Uses per-city same-day cutoff.
    """
    today = timezone.localdate()
    
    if _same_day_allowed(city=destination_city, delivery_date=today):
        return {
            "label": "Today",
            "delivery_date": today.isoformat(),
            "is_same_day": True,
            "city": destination_city.name,
        }

    tomorrow = today + timedelta(days=1)
    return {
        "label": "Tomorrow",
        "delivery_date": tomorrow.isoformat(),
        "is_same_day": False,
        "city": destination_city.name,
    }


def get_delivery_charge(*, item_count: int, destination_city: City) -> Decimal:
    """
    Return delivery charge for a cart heading to a destination city.

    Uses the city's ``delivery_charge_base`` field.
    """
    if item_count <= 0:
        return Decimal("0.00")
    return destination_city.delivery_charge_base
