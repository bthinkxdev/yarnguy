"""Order management: filterable list, detail, and guarded status transitions."""

from __future__ import annotations

from django.contrib import messages
from django.core.paginator import Paginator
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from dashboard.access import dashboard_required
from orders.exceptions import InvalidOrderStatusTransitionError
from orders.models import Order, OrderStatus
from orders.services import ALLOWED_STATUS_TRANSITIONS, transition_order_status

_STATUS_LABELS = dict(OrderStatus.choices)


@dashboard_required
@require_http_methods(["GET"])
def order_list(request: HttpRequest) -> HttpResponse:
    """Paginated, status-filterable, searchable order list."""
    qs = Order.objects.select_related("customer_profile__user", "currency").order_by("-created_at")

    status = request.GET.get("status", "").strip()
    if status:
        qs = qs.filter(order_status=status)
    from django.db.models import Q
    query = request.GET.get("q", "").strip()
    if query:
        qs = qs.filter(
            Q(order_number__icontains=query) |
            Q(customer_profile__user__first_name__icontains=query) |
            Q(customer_profile__user__last_name__icontains=query) |
            Q(customer_profile__phone__icontains=query)
        )

    from payments.models import PaymentStatus
    payment_status = request.GET.get("payment_status", "").strip()
    if payment_status:
        qs = qs.filter(payment_transactions__status=payment_status).distinct()

    paginator = Paginator(qs, 25)
    page_obj = paginator.get_page(request.GET.get("page"))

    params = request.GET.copy()
    params.pop("page", None)

    def _clear_url(param: str) -> str:
        p = request.GET.copy()
        p.pop("page", None)
        p.pop(param, None)
        return f"{request.path}?{p.urlencode()}" if p else request.path

    active_filters = []
    if status:
        active_filters.append({"label": _STATUS_LABELS.get(status, status), "clear_url": _clear_url("status")})
    if payment_status:
        active_filters.append({
            "label": f"Payment: {dict(PaymentStatus.choices).get(payment_status, payment_status)}",
            "clear_url": _clear_url("payment_status"),
        })
    if query:
        active_filters.append({"label": f'Search: "{query}"', "clear_url": _clear_url("q")})

    context = {
        "nav_section": "orders",
        "page_title": "Orders",
        "page_obj": page_obj,
        "objects": page_obj.object_list,
        "statuses": OrderStatus.choices,
        "payment_statuses": PaymentStatus.choices,
        "current_status": status,
        "current_payment_status": payment_status,
        "search_query": query,
        "querystring": params.urlencode(),
        "active_filters": active_filters,
    }
    return render(request, "dashboard/orders/list.html", context)


@dashboard_required
@require_http_methods(["GET"])
def order_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Order detail with line items, status history, POD and payments."""
    order = get_object_or_404(
        Order.objects.select_related("customer_profile__user", "currency"), pk=pk
    )
    items = order.items.select_related("product", "variant").all()
    history = order.status_history.select_related("changed_by").all()
    payments = order.payment_transactions.select_related("currency").all()
    pod = getattr(order, "proof_of_delivery", None)

    #only the actually-valid next statuses — CONFIRMED is the sole trigger for AWB
    #creation, so an admin must never be able to pick an arbitrary status here.
    next_statuses = ALLOWED_STATUS_TRANSITIONS.get(order.order_status, set())
    allowed_choices = [(value, _STATUS_LABELS[value]) for value in next_statuses]
    from payments.models import PaymentStatus
    payment_statuses = PaymentStatus.choices

    context = {
        "nav_section": "orders",
        "page_title": f"Order {order.order_number}",
        "order": order,
        "items": items,
        "history": history,
        "payments": payments,
        "pod": pod,
        "allowed_choices": allowed_choices,
        "payment_statuses": payment_statuses,
    }
    return render(request, "dashboard/orders/detail.html", context)


@dashboard_required
@require_POST
def order_transition(request: HttpRequest, pk: int) -> HttpResponse:
    """Apply a status transition through the orders service state machine."""
    order = get_object_or_404(Order, pk=pk)
    new_status = request.POST.get("new_status", "")
    note = request.POST.get("note", "")
    try:
        transition_order_status(
            order=order, new_status=new_status, actor=request.user, note=note, force=False
        )
        messages.success(
            request, f"Order moved to {dict(OrderStatus.choices).get(new_status, new_status)}."
        )
    except InvalidOrderStatusTransitionError as exc:
        messages.error(request, str(exc))
    return redirect("dashboard:order-detail", pk=pk)


@dashboard_required
@require_POST
def order_payment_transition(request: HttpRequest, pk: int) -> HttpResponse:
    """Manually update the payment status of the latest payment transaction."""
    from payments.models import PaymentStatus
    
    order = get_object_or_404(Order, pk=pk)
    new_status = request.POST.get("new_status", "")
    
    # get the latest payment transaction to update
    tx = order.payment_transactions.last()
    
    if tx and new_status in dict(PaymentStatus.choices):
        if new_status == PaymentStatus.SUCCESS:
            from payments.services import confirm_payment_success
            confirm_payment_success(payment_transaction=tx)
            messages.success(request, f"Payment marked as {dict(PaymentStatus.choices).get(new_status)}.")
        elif new_status == PaymentStatus.FAILED:
            from payments.services import confirm_payment_failed
            confirm_payment_failed(payment_transaction=tx)
            messages.success(request, f"Payment marked as {dict(PaymentStatus.choices).get(new_status)}.")
        else:
            tx.status = new_status
            tx.save(update_fields=["status", "updated_at"])
            messages.success(request, f"Payment marked as {dict(PaymentStatus.choices).get(new_status)}.")
    elif not tx:
        messages.error(request, "Could not update payment status: No payment records found.")
        
    return redirect("dashboard:order-detail", pk=pk)


@dashboard_required
@require_http_methods(["GET"])
def order_invoice_detail(request: HttpRequest, pk: int) -> HttpResponse:
    """Render the HTML invoice for an order."""
    from core.models import SiteSettings
    order = get_object_or_404(
        Order.objects.select_related("customer_profile__user", "currency"), pk=pk
    )
    
    context = {
        "order": order,
        "site_settings": SiteSettings.objects.first(),
    }
    return render(request, "shared/order_invoice.html", context)


@dashboard_required
@require_http_methods(["POST"])
def order_bulk_invoice_detail(request: HttpRequest) -> HttpResponse:
    """Render the HTML bulk invoice for selected orders."""
    from core.models import SiteSettings
    
    order_ids = request.POST.getlist("order_ids")
    if not order_ids:
        messages.error(request, "No orders selected for printing.")
        return redirect("dashboard:order-list")
        
    orders_qs = Order.objects.select_related(
        "customer_profile__user", "currency"
    ).filter(pk__in=order_ids).order_by("-created_at")
    
    context = {
        "orders": orders_qs,
        "site_settings": SiteSettings.objects.first(),
    }
    return render(request, "shared/order_bulk_invoice.html", context)
