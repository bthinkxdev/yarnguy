"""Read-only query functions for the reports app."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Optional

from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Count, Sum
from django.utils import timezone

from orders.models import Order, OrderStatus
from reports.models import (
    DailyCustomerReport,
    DailyProductPerformance,
    DailySalesReport,
    InventorySnapshot,
)

ADMIN_DASHBOARD_CACHE_KEY = "reports:admin_dashboard:today"
ADMIN_DASHBOARD_CACHE_TTL = 300


def get_daily_sales_reports(
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    page: int = 1,
    page_size: int = 30,
) -> dict[str, Any]:
    """Paginated daily sales from pre-aggregated table only."""
    qs = DailySalesReport.objects.all()
    if start_date:
        qs = qs.filter(report_date__gte=start_date)
    if end_date:
        qs = qs.filter(report_date__lte=end_date)
    paginator = Paginator(qs.order_by("-report_date"), page_size)
    page_obj = paginator.get_page(page)
    return {
        "results": list(page_obj.object_list),
        "page": page_obj.number,
        "total_count": paginator.count,
        "has_next": page_obj.has_next(),
    }


def get_daily_product_performance(
    *,
    report_date: date,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Product performance for a single day from pre-aggregated table."""
    qs = DailyProductPerformance.objects.filter(report_date=report_date).select_related("product")
    paginator = Paginator(qs.order_by("-revenue"), page_size)
    page_obj = paginator.get_page(page)
    return {"results": list(page_obj.object_list), "page": page_obj.number}


def get_daily_customer_reports(
    *,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    page: int = 1,
    page_size: int = 30,
) -> dict[str, Any]:
    """Paginated customer reports from pre-aggregated table."""
    qs = DailyCustomerReport.objects.all()
    if start_date:
        qs = qs.filter(report_date__gte=start_date)
    if end_date:
        qs = qs.filter(report_date__lte=end_date)
    paginator = Paginator(qs.order_by("-report_date"), page_size)
    page_obj = paginator.get_page(page)
    return {"results": list(page_obj.object_list), "page": page_obj.number}


def get_inventory_snapshots(
    *,
    report_date: date,
    low_stock_only: bool = False,
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    """Inventory snapshot for a date from pre-aggregated table."""
    qs = InventorySnapshot.objects.filter(report_date=report_date).select_related("product")
    if low_stock_only:
        qs = qs.filter(is_low_stock=True)
    paginator = Paginator(qs.order_by("product__name"), page_size)
    page_obj = paginator.get_page(page)
    return {"results": list(page_obj.object_list), "page": page_obj.number}


def get_admin_dashboard_summary() -> dict[str, Any]:
    """
    Admin dashboard summary.

    Historical data reads pre-aggregated tables. Today's revenue/order count
    is a deliberate live exception (today cannot be pre-aggregated yet) —
    cached 5 minutes to bound query cost.
    """
    cached = cache.get(ADMIN_DASHBOARD_CACHE_KEY)
    if cached is not None:
        return cached

    today = timezone.localdate()
    yesterday = today - timedelta(days=1)

    yesterday_report = DailySalesReport.objects.filter(report_date=yesterday).first()
    low_stock_count = InventorySnapshot.objects.filter(
        report_date=yesterday,
        is_low_stock=True,
    ).count()

    today_orders = Order.objects.filter(created_at__date=today).exclude(
        order_status=OrderStatus.CANCELLED,
    )
    today_agg = today_orders.aggregate(
        order_count=Count("id"),
        revenue=Sum("total_amount"),
    )

    summary = {
        "today_revenue": today_agg["revenue"] or 0,
        "today_order_count": today_agg["order_count"] or 0,
        "yesterday_revenue": yesterday_report.revenue if yesterday_report else 0,
        "yesterday_order_count": yesterday_report.order_count if yesterday_report else 0,
        "low_stock_alert_count": low_stock_count,
    }
    cache.set(ADMIN_DASHBOARD_CACHE_KEY, summary, ADMIN_DASHBOARD_CACHE_TTL)
    return summary
