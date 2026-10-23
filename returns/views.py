"""Customer-facing return pages (request, list, detail, cancel)."""

from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from orders.models import Order
from returns.models import ReturnReason, ReturnRequest, ReturnResolution
from returns.services import ReturnError, cancel_return_request, create_return_request, get_eligibility


def _customer_order(request: HttpRequest, order_id: int) -> Order:
    return get_object_or_404(
        Order.objects.select_related("customer_profile"),
        pk=order_id,
        customer_profile=request.user.customer_profile,
    )


def _parse_selections(post) -> list[tuple[int, int, str]]:
    """``qty_<order item id>`` / ``reason_<order item id>`` form fields -> (id, quantity, reason)."""
    selections = []
    for key, value in post.items():
        if not key.startswith("qty_"):
            continue
        try:
            item_id, quantity = int(key[4:]), int(value or 0)
        except ValueError:
            raise ReturnError("Enter a valid quantity.")
        if quantity < 0:
            raise ReturnError("Enter a valid quantity.")
        selections.append((item_id, quantity, post.get(f"reason_{item_id}", "")))
    return selections


@login_required
@require_http_methods(["GET", "POST"])
def return_request_view(request: HttpRequest, order_id: int) -> HttpResponse:
    """Choose items, quantities and a reason for each, and submit a return request."""
    order = _customer_order(request, order_id)
    eligibility = get_eligibility(order)

    if request.method == "POST":
        try:
            created = create_return_request(
                order=order,
                customer_profile=request.user.customer_profile,
                selections=_parse_selections(request.POST),
                resolution=request.POST.get("resolution", ""),
                note=request.POST.get("note", ""),
            )
        except ReturnError as exc:
            messages.error(request, str(exc))
            eligibility = get_eligibility(order)  # may have changed under us
        else:
            messages.success(request, f"Return request {created.return_number} submitted.")
            return redirect("returns:detail", pk=created.pk)

    preselected = request.GET.get("item", "")
    posted = request.POST if request.method == "POST" else {}
    rows = [
        {
            "entry": entry,
            "quantity": posted.get(f"qty_{entry.order_item.pk}") or ("1" if preselected == str(entry.order_item.pk) else "0"),
            "reason": posted.get(f"reason_{entry.order_item.pk}", ""),
        }
        for entry in eligibility.items
    ]
    return render(
        request,
        "returns/request_form.html",
        {
            "order": order,
            "eligibility": eligibility,
            "rows": rows,
            "reasons": ReturnReason.choices,
            "resolutions": ReturnResolution.choices,
            "resolution": posted.get("resolution", ReturnResolution.REFUND),
            "note": posted.get("note", ""),
        },
    )


@login_required
@require_GET
def return_list_view(request: HttpRequest) -> HttpResponse:
    returns = (
        ReturnRequest.objects.filter(customer_profile=request.user.customer_profile)
        .select_related("order")
        .prefetch_related("items__order_item__product")
    )
    return render(request, "returns/list.html", {"returns": returns})


@login_required
@require_GET
def return_detail_view(request: HttpRequest, pk: int) -> HttpResponse:
    return_request = (
        ReturnRequest.objects.select_related("order")
        .prefetch_related("items__order_item__product", "items__order_item__variant")
        .filter(pk=pk, customer_profile=request.user.customer_profile)
        .first()
    )
    if return_request is None:
        raise Http404("Return not found.")
    return render(request, "returns/detail.html", {"return_request": return_request})


@login_required
@require_POST
def return_cancel_view(request: HttpRequest, pk: int) -> HttpResponse:
    return_request = get_object_or_404(ReturnRequest, pk=pk, customer_profile=request.user.customer_profile)
    try:
        cancel_return_request(return_request, customer_profile=request.user.customer_profile)
        messages.success(request, "Your return request was cancelled.")
    except ReturnError as exc:
        messages.error(request, str(exc))
    return redirect("returns:detail", pk=pk)
