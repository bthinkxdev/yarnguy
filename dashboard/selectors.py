"""Read-only query helpers for the admin dashboard (home + reports charts)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import F
from django.utils import timezone

from accounts.models import CustomerProfile
from catalog.models import Product
from orders.models import Order
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


def get_customer_split() -> dict[str, list[float]]:
    """Return [new%, returning%] over the last 30 days (true unique human count)."""
    start = timezone.localdate() - timedelta(days=30)
    
    # 1.how many brand new accounts were created in the last 30 days?
    from accounts.models import CustomerProfile
    new_customers = CustomerProfile.objects.filter(created_at__date__gte=start).count()
    
    # 2.how many unique people placed an order in the last 30 days?
    from orders.models import Order
    unique_buyers = Order.objects.filter(
        created_at__date__gte=start
    ).values("customer_profile").distinct().count()
    
    # returning = people who bought minus the newly created accounts
    returning_customers = max(0, unique_buyers - new_customers)
    
    total = new_customers + returning_customers
    if total == 0:
        return {"series": [0, 0]}
        
    new_pct = round(100 * new_customers / total)
    return {"series": [new_pct, 100 - new_pct]}


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
    """Top products by revenue on the most recent day that has performance data."""
    latest = (
        DailyProductPerformance.objects.order_by("-report_date")
        .values_list("report_date", flat=True)
        .first()
    )
    if not latest:
        return []
    rows = list(
        DailyProductPerformance.objects.filter(report_date=latest)
        .select_related("product", "product__category")
        .prefetch_related("product__images")
        .order_by("-revenue")[:limit]
    )
    top_revenue = float(rows[0].revenue) if rows and rows[0].revenue else 0.0
    result: list[dict[str, Any]] = []
    for r in rows:
        rev = float(r.revenue or 0)
        result.append(
            {
                "name": r.product.name,
                "units": r.units_sold,
                "revenue": r.revenue,
                "category": r.product.category.name if r.product.category_id else "",
                "image": _primary_image_url(r.product),
                "share": round(100 * rev / top_revenue) if top_revenue else 0,
            }
        )
    return result


def get_low_stock_products(*, limit: int = 5) -> list[dict[str, Any]]:
    """Active products at or below their low-stock threshold."""
    products = (
        Product.objects.filter(is_active=True, stock_quantity__lte=F("low_stock_threshold"))
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
    """Most recent orders with their customer preloaded."""
    return list(
        Order.objects.select_related("customer_profile__user", "currency").order_by("-created_at")[
            :limit
        ]
    )


def get_dashboard_counts() -> dict[str, int]:
    """Cheap top-level counts for the overview widget."""
    return {
        "product_count": Product.objects.filter(is_active=True).count(),
        "customer_count": CustomerProfile.objects.count(),
        "order_count": Order.objects.count(),
    }
