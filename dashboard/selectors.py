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
    """Top products by live all-time revenue."""
    from django.db.models import Sum, F
    from orders.models import OrderItem, OrderStatus
    
    rows = list(
        OrderItem.objects.exclude(order__order_status=OrderStatus.CANCELLED)
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
