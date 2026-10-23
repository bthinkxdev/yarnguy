"""Dashboard screens for managing customer returns and their reverse pickups."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from dashboard.access import dashboard_required
from indiapost.services import PICKUP_SLOTS
from returns import services
from returns.models import ReturnRequest, ReturnStatus
from returns.services import ReturnError


@dashboard_required
@require_GET
def return_list(request: HttpRequest) -> HttpResponse:
    """All return requests, newest first, filterable by status and searchable by return / order number."""
    queryset = ReturnRequest.objects.select_related("order", "customer_profile__user").prefetch_related(
        "items__order_item__product"
    )
    status = request.GET.get("status", "").strip()
    if status in ReturnStatus.values:
        queryset = queryset.filter(status=status)
    search = request.GET.get("q", "").strip()
    if search:
        queryset = queryset.filter(
            Q(return_number__icontains=search)
            | Q(order__order_number__icontains=search)
            | Q(reverse_tracking_number__icontains=search)
        )
    page = Paginator(queryset, 25).get_page(request.GET.get("page"))
    return render(
        request,
        "dashboard/returns/list.html",
        {
            "nav_section": "returns",
            "page_title": "Returns",
            "page_obj": page,
            "returns": page.object_list,
            "statuses": ReturnStatus.choices,
            "current_status": status,
            "search_query": search,
            "open_count": ReturnRequest.objects.filter(status=ReturnStatus.REQUESTED).count(),
        },
    )


@dashboard_required
@require_GET
def return_detail(request: HttpRequest, pk: int) -> HttpResponse:
    return_request = get_object_or_404(
        ReturnRequest.objects.select_related("order", "customer_profile__user").prefetch_related(
            "items__order_item__product", "items__order_item__variant"
        ),
        pk=pk,
    )
    return render(
        request,
        "dashboard/returns/detail.html",
        {
            "nav_section": "returns",
            "page_title": f"Return {return_request.return_number}",
            "r": return_request,
            "order": return_request.order,
            "slots": list(PICKUP_SLOTS),
            "default_day": (return_request.pickup_day or timezone.localdate() + timedelta(days=1)).isoformat(),
            "default_slot": return_request.pickup_slot,
            "min_day": timezone.localdate().isoformat(),
            "suggested_refund": return_request.items_value,
        },
    )


def _parse_day(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ReturnError("Enter a valid pickup date.")


def _approve(r: ReturnRequest, post) -> str:
    services.approve_return(r)
    if r.pickup_is_app_managed:
        return "Return approved. Use 'Initiate pickup' to book the India Post collection."
    return "Return approved. Mark it received once the items are back with you."


def _initiate_pickup(r: ReturnRequest, post) -> str:
    updated = services.initiate_pickup(
        r, pickup_day=_parse_day(post.get("pickup_day", "")), pickup_slot=post.get("pickup_slot", "")
    )
    if updated.status == ReturnStatus.PICKUP_SCHEDULED:
        return f"Pickup initiated - tracking {updated.reverse_tracking_number}."
    raise ReturnError(
        f"The pickup could not be booked: {updated.pickup_error} "
        "Fix the problem and try again, or enter a tracking number by hand."
    )


def _manual_pickup(r: ReturnRequest, post) -> str:
    raw_day = post.get("pickup_day", "").strip()
    services.enter_pickup_manually(
        r,
        tracking_number=post.get("tracking_number", ""),
        pickup_day=_parse_day(raw_day) if raw_day else None,
    )
    return "Pickup details saved."


def _received(r: ReturnRequest, post) -> str:
    resellable = [int(v) for v in post.getlist("resellable") if v.isdigit()]
    services.mark_received(r, resellable=resellable)
    return f"Marked received; {len(resellable)} line(s) went back into stock."


def _refunded(r: ReturnRequest, post) -> str:
    raw = post.get("amount", "").strip()
    try:
        amount = Decimal(raw) if raw else None
    except InvalidOperation:
        raise ReturnError("Enter a valid refund amount.")
    services.mark_refunded(r, amount=amount)
    return "Refund recorded."


def _replaced(r: ReturnRequest, post) -> str:
    services.mark_replaced(r, tracking_number=post.get("tracking_number", ""))
    return "Replacement recorded and the items were taken out of stock."


ACTIONS = {
    "approve": _approve,
    "reject": lambda r, post: (services.reject_return(r, reason=post.get("reason", "")), "Return rejected.")[1],
    "initiate-pickup": _initiate_pickup,
    "manual-pickup": _manual_pickup,
    "picked-up": lambda r, post: (services.mark_picked_up(r), "Marked as picked up.")[1],
    "received": _received,
    "refunded": _refunded,
    "replaced": _replaced,
}


@dashboard_required
@require_POST
def return_action(request: HttpRequest, pk: int, action: str) -> HttpResponse:
    """Apply one lifecycle action to a return; every rule is enforced in ``returns.services``."""
    handler = ACTIONS.get(action)
    if handler is None:
        raise Http404("Unknown action.")
    return_request = get_object_or_404(ReturnRequest, pk=pk)
    try:
        messages.success(request, handler(return_request, request.POST))
    except ReturnError as exc:
        messages.error(request, str(exc))
    return redirect("dashboard:return-detail", pk=pk)
