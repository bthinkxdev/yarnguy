"""Reports & analytics: charts, tables, CSV export, and manual recompute."""

from __future__ import annotations

import csv
from datetime import date, datetime, timedelta

from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from dashboard.access import dashboard_required
from reports.selectors import (
    get_admin_dashboard_summary,
    get_daily_customer_reports,
    get_daily_sales_reports,
)
from reports.services import aggregate_daily_reports


def _parse_date(value: str, default: date) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return default


@dashboard_required
def reports_view(request: HttpRequest) -> HttpResponse:
    """Analytics dashboard over the pre-aggregated report tables."""
    today = timezone.localdate()
    end = _parse_date(request.GET.get("end", ""), today)
    start = _parse_date(request.GET.get("start", ""), today - timedelta(days=29))

    sales = get_daily_sales_reports(start_date=start, end_date=end, page=1, page_size=366)
    customers = get_daily_customer_reports(start_date=start, end_date=end, page=1, page_size=366)

    ordered = list(reversed(sales["results"]))
    chart = {
        "categories": [r.report_date.strftime("%b %d") for r in ordered],
        "revenue": [float(r.revenue) for r in ordered],
        "orders": [r.order_count for r in ordered],
    }
    total_revenue = sum(float(r.revenue) for r in sales["results"])
    total_orders = sum(r.order_count for r in sales["results"])

    context = {
        "nav_section": "reports",
        "page_title": "Reports",
        "start": start,
        "end": end,
        "summary": get_admin_dashboard_summary(),
        "sales": sales["results"],
        "customers": customers["results"],
        "sales_series": chart,
        "total_revenue": total_revenue,
        "total_orders": total_orders,
    }
    return render(request, "dashboard/reports/index.html", context)


@dashboard_required
def reports_export_csv(request: HttpRequest) -> HttpResponse:
    """Export daily sales in the selected range as CSV."""
    today = timezone.localdate()
    end = _parse_date(request.GET.get("end", ""), today)
    start = _parse_date(request.GET.get("start", ""), today - timedelta(days=29))
    sales = get_daily_sales_reports(start_date=start, end_date=end, page=1, page_size=366)

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="sales_{start}_{end}.csv"'
    writer = csv.writer(response)
    writer.writerow(["Date", "Orders", "Revenue", "Avg order value", "Coupon discount"])
    for r in sales["results"]:
        writer.writerow(
            [
                r.report_date,
                r.order_count,
                r.revenue,
                r.average_order_value,
                r.coupon_discount_total,
            ]
        )
    return response


@dashboard_required
@require_POST
def reports_recompute(request: HttpRequest) -> HttpResponse:
    """Manually re-run aggregation for a given date (default: yesterday)."""
    target = _parse_date(request.POST.get("date", ""), timezone.localdate() - timedelta(days=1))
    try:
        aggregate_daily_reports(report_date=target)
        messages.success(request, f"Reports recomputed for {target}.")
    except Exception as exc:  # noqa: BLE001 - surface any aggregation failure to admin
        messages.error(request, f"Recompute failed: {exc}")
    return redirect("dashboard:reports")
