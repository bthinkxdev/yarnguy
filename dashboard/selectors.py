"""Read-only query helpers for the admin dashboard (home + reports charts)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from typing import Any

from django.db.models import Count, F, Min, Q, QuerySet
from django.utils import timezone

from accounts.models import CustomerProfile
from catalog.models import Product
from orders.models import Order, OrderStatus
from orders.services import REVENUE_ORDER_STATUSES
from reports.models import (
    DailyCustomerReport,
    DailyProductPerformance,
    DailySalesReport,
)


def get_sales_series(*, days: int = 14) -> dict[str, list]:
    """Return ordered date labels, revenue and order counts for the last N days."""
    start = timezone.localdate() - timedelta(days=days - 1)
    rows = {
        r.report_date: r
        for r in DailySalesReport.objects.filter(report_date__gte=start).order_by("report_date")
    }
    
    from reports.selectors import get_live_today_sales_report
    today = timezone.localdate()
    rows[today] = get_live_today_sales_report()
    categories: list[str] = []
    revenue: list[float] = []
    orders: list[int] = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        categories.append(day.strftime("%b %d"))
        row = rows.get(day)
        revenue.append(float(row.revenue) if row else 0.0)
        orders.append(row.order_count if row else 0)
    return {"categories": categories, "revenue": revenue, "orders": orders}


#Customer-overview reporting windows: key -> number of calendar days (None = all time).
CUSTOMER_PERIODS: dict[str, int | None] = {"7": 7, "30": 30, "90": 90, "all": None}
CUSTOMER_PERIOD_LABELS: dict[str, str] = {
    "7": "7 days",
    "30": "30 days",
    "90": "90 days",
    "all": "All time",
}
DEFAULT_CUSTOMER_PERIOD = "30"


def qualifying_orders() -> QuerySet[Order]:
    """
    Orders that count as a real sale on the customer overview.

    ``success_at`` is set exactly once, when an order first becomes a sale: online
    payment confirmed, or COD placed. Unpaid / abandoned checkouts never get it.
    Cancelled and refunded orders had that sale reversed, so they are excluded.
    """
    return Order.objects.filter(success_at__isnull=False).exclude(
        order_status__in=[OrderStatus.CANCELLED, OrderStatus.REFUNDED]
    )


def resolve_customer_period(raw: str | None) -> str:
    return raw if raw in CUSTOMER_PERIODS else DEFAULT_CUSTOMER_PERIOD


def customer_period_start(period: str) -> datetime | None:
    """
    Start of the reporting window as an aware datetime, or None for all time.

    A period of N days is today plus the previous N-1 calendar days, from local
    midnight — the same timezone.localdate() day boundary the sales chart uses.
    """
    days = CUSTOMER_PERIODS[period]
    if days is None:
        return None
    first_day = timezone.localdate() - timedelta(days=days - 1)
    return timezone.make_aware(datetime.combine(first_day, time.min))


def get_customer_split(period: str = DEFAULT_CUSTOMER_PERIOD) -> dict[str, Any]:
    """
    New vs returning customers among those with a qualifying order in ``period``.

    * New       — first-ever qualifying order falls inside the period and they placed
                  exactly one in it.
    * Returning — had a qualifying order before the period, or two or more inside it.
    * Customers with no qualifying order in the period are excluded entirely, so the
      percentages always come from the same eligible population and total 100.

    For "all time" there is no prior history, so it reduces to one order = new,
    two or more = returning.
    """
    start = customer_period_start(period)
    in_period = Count("id", filter=Q(success_at__gte=start)) if start else Count("id")
    rows = (
        qualifying_orders()
        .filter(customer_profile__isnull=False)
        .values("customer_profile")
        .annotate(first_success=Min("success_at"), in_period=in_period)
    )

    new_customers = returning_customers = 0
    for row in rows:
        if row["in_period"] == 0:
            continue
        had_prior_order = start is not None and row["first_success"] < start
        if had_prior_order or row["in_period"] >= 2:
            returning_customers += 1
        else:
            new_customers += 1

    total = new_customers + returning_customers
    new_pct = round(100 * new_customers / total) if total else 0
    return {
        "series": [new_pct, 100 - new_pct] if total else [0, 0],
        "new_count": new_customers,
        "returning_count": returning_customers,
    }


def _primary_image_url(product: Product) -> str | None:
    """Best-effort primary image URL for a product (images assumed prefetched)."""
    images = list(product.images.all())
    if not images:
        return None
    primary = next((im for im in images if im.is_primary), images[0])
    try:
        return primary.image.url if primary.image else None
    except ValueError:
        return None


def get_top_products(*, limit: int = 5) -> list[dict[str, Any]]:
    """Top products by live all-time revenue."""
    from django.db.models import Sum, F
    from orders.models import OrderItem

    rows = list(
        OrderItem.objects.filter(order__order_status__in=REVENUE_ORDER_STATUSES)
        .values("product_id", "product__name", "product__category__name", "product__category_id")
        .annotate(
            total_units=Sum("quantity"),
            total_revenue=Sum(F("unit_price") * F("quantity"))
        )
        .filter(total_revenue__gt=0)
        .order_by("-total_revenue")[:limit]
    )
    if not rows:
        return []

    product_ids = [r["product_id"] for r in rows]
    products = Product.objects.filter(id__in=product_ids).prefetch_related("images")
    product_map = {p.id: p for p in products}

    top_revenue = float(rows[0]["total_revenue"]) if rows else 0.0
    result: list[dict[str, Any]] = []
    
    for r in rows:
        rev = float(r["total_revenue"] or 0)
        product = product_map.get(r["product_id"])
        result.append(
            {
                "name": r["product__name"],
                "units": r["total_units"],
                "revenue": r["total_revenue"],
                "category": r["product__category__name"] if r["product__category_id"] else "",
                "image": _primary_image_url(product) if product else None,
                "share": round(100 * rev / top_revenue) if top_revenue else 0,
            }
        )
    return result


def get_low_stock_products(*, limit: int = 5) -> list[dict[str, Any]]:
    """Active products at or below their low-stock threshold (but not completely out of stock)."""
    products = (
        Product.objects.filter(is_active=True, stock_quantity__lte=F("low_stock_threshold"), stock_quantity__gt=0)
        .select_related("category")
        .prefetch_related("images")
        .order_by("stock_quantity")[:limit]
    )
    return [
        {
            "name": p.name,
            "sku": p.sku,
            "stock": p.stock_quantity,
            "category": p.category.name if p.category_id else "",
            "image": _primary_image_url(p),
        }
        for p in products
    ]


def get_recent_orders(*, limit: int = 6) -> list[Order]:
    """Most recently succeeded orders with their customer preloaded.

    Sorted by success_at (set once, when the order first becomes a real sale) so a
    late/retried payment success resurfaces the order here even though it was
    originally placed days or weeks ago, without reshuffling on unrelated later
    activity (a courier scan, an address edit) — see orders.models.Order.Meta.ordering.
    """
    return list(
        Order.objects.select_related("customer_profile__user", "currency")
        .order_by(F("success_at").desc(nulls_last=True), "-created_at")[:limit]
    )


def get_dashboard_counts(period: str = DEFAULT_CUSTOMER_PERIOD) -> dict[str, int]:
    """
    Top-level counts for the overview widget.

    ``qualifying_order_count`` uses the same definition and window as the customer
    donut; ``qualifying_order_count_all_time`` is the lifetime figure for the KPI tile.
    """
    start = customer_period_start(period)
    orders = qualifying_orders()
    return {
        "product_count": Product.objects.filter(is_active=True).count(),
        "registered_customer_count": CustomerProfile.objects.count(),
        "qualifying_order_count": (
            orders.filter(success_at__gte=start) if start else orders
        ).count(),
        "qualifying_order_count_all_time": orders.count(),
    }
