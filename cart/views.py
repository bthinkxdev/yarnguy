"""HTTP views for the cart app."""

from __future__ import annotations

import json

from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from cart.exceptions import CartItemNotFoundError, InsufficientStockError
from cart.forms import CartCouponForm, CartQuantityForm
from cart.selectors import get_cart_count, get_cart_for_request, get_cart_summary, get_wishlist_count, get_cart_product_ids
from cart.services import (
    add_to_cart,
    adjust_cart_item_quantity,
    apply_coupon,
    get_or_create_cart,
    remove_coupon,
    remove_cart_item,
    toggle_wishlist,
)
from catalog.selectors import get_product_for_cart_add
from marketing.exceptions import InvalidCouponError


def _cart_drawer_response(request: HttpRequest, *, error: str | None = None, error_item_id: int | None = None, hx_triggers: dict | None = None) -> HttpResponse:
    """Render cart drawer partial; optionally attach HTMX trigger headers."""
    cart = get_cart_for_request(request=request)
    summary = get_cart_summary(cart=cart) if cart else None
    from marketing.selectors import has_any_active_coupons
    response = render(
        request,
        "cart/partials/drawer.html",
        {
            "summary": summary,
            "cart_count": summary.item_count if summary else 0,
            "has_active_coupons": has_any_active_coupons(),
            "error": error,
            "error_item_id": error_item_id,
        },
    )
    if hx_triggers:
        response["HX-Trigger"] = json.dumps(hx_triggers)
    return response


@require_GET
def cart_drawer_view(request: HttpRequest) -> HttpResponse:
    """HTMX partial for the cart drawer."""
    return _cart_drawer_response(request)


def _cart_page_response(
    request: HttpRequest, *, error: str | None = None, error_item_id: int | None = None, hx_triggers: dict | None = None
) -> HttpResponse:
    """Render the standalone cart page's swappable body; optionally attach HTMX triggers."""
    cart = get_cart_for_request(request=request)
    summary = get_cart_summary(cart=cart) if cart else None
    from marketing.selectors import has_any_active_coupons
    response = render(
        request,
        "cart/partials/page_body.html",
        {
            "summary": summary,
            "cart_count": summary.item_count if summary else 0,
            "error": error,
            "error_item_id": error_item_id,
            "has_active_coupons": has_any_active_coupons(),
        },
    )
    if hx_triggers:
        response["HX-Trigger"] = json.dumps(hx_triggers)
    return response


@require_GET
def cart_page_view(request: HttpRequest) -> HttpResponse:
    """Standalone cart page — full item list, coupon box, and order summary."""
    cart = get_or_create_cart(request=request)
    summary = get_cart_summary(cart=cart)
    from marketing.selectors import has_any_active_coupons
    return render(
        request,
        "cart/cart_page.html",
        {
            "summary": summary,
            "cart_count": summary.item_count,
            "has_active_coupons": has_any_active_coupons(),
        },
    )


@require_GET
def cart_count_view(request: HttpRequest) -> HttpResponse:
    """HTMX partial for the header cart badge — lightweight COUNT only."""
    return render(
        request,
        "cart/partials/count_badge.html",
        {"count": get_cart_count(request=request)},
    )


@require_GET
def cart_status_view(request: HttpRequest) -> JsonResponse:
    """JSON endpoint returning active cart count, product IDs, and item keys for JS state sync."""
    from cart.models import CartItem
    cart = get_cart_for_request(request=request)
    product_ids = set()
    cart_item_keys = []
    count = 0
    if cart:
        for pid, vid, qty in CartItem.objects.filter(cart=cart).values_list("product_id", "variant_id", "quantity"):
            product_ids.add(pid)
            cart_item_keys.append(f"{pid}_{vid}" if vid else f"{pid}")
            count += qty
    return JsonResponse({
        "cart_product_ids": list(product_ids),
        "cart_count": count,
        "cart_item_keys": cart_item_keys,
    })


@require_GET
def wishlist_count_view(request: HttpRequest) -> HttpResponse:
    """HTMX partial for wishlist badges — lightweight COUNT only."""
    return render(
        request,
        "cart/partials/wishlist_count_badge.html",
        {"count": get_wishlist_count(request=request)},
    )


@require_POST
def cart_add_view(request: HttpRequest) -> HttpResponse:
    """Add product to persistent cart and return drawer partial or redirect to checkout."""
    product_id = int(request.POST.get("product_id", 0))
    quantity = int(request.POST.get("quantity", 1))
    variant_id_raw = request.POST.get("variant_id")
    variant_id = int(variant_id_raw) if variant_id_raw else None

    product, variant = get_product_for_cart_add(product_id=product_id, variant_id=variant_id)
    if product is None:
        raise Http404("Product not found.")

    cart = get_or_create_cart(request=request)

    buy_now = request.POST.get("buy_now") == "true"
    
    try:
        new_item = add_to_cart(
            cart=cart,
            product=product,
            variant=variant,
            quantity=quantity,
            overwrite=True,
        )
    except InsufficientStockError as exc:
        from django.contrib import messages
        messages.error(request, str(exc))
        if request.headers.get("HX-Request") and not buy_now:
            return _cart_drawer_response(request, error=str(exc))
        # If buy_now or not htmx, redirect back to referer
        return redirect(request.META.get("HTTP_REFERER", "/"))

    if buy_now:
        from django.urls import reverse
        checkout_url = reverse("checkout:checkout")
        if request.headers.get("HX-Request"):
            response = HttpResponse(status=204)
            response["HX-Redirect"] = checkout_url
            return response
        return redirect("checkout:checkout")

    return _cart_drawer_response(
        request,
        hx_triggers={"cartItemAdded": {"product_id": product.pk}},
    )


@require_POST
def cart_remove_view(request: HttpRequest) -> HttpResponse:
    """Remove a cart line and return drawer partial."""
    cart = get_cart_for_request(request=request)
    removed_product_id = None
    if cart:
        removed_product_id = remove_cart_item(cart=cart, cart_item_id=int(request.POST.get("cart_item_id", 0)))
    
    triggers = {"cartUpdated": None}
    if removed_product_id:
        triggers["cartItemRemoved"] = {"product_id": removed_product_id}
        
    return _cart_drawer_response(request, hx_triggers=triggers)


@require_POST
def cart_page_remove_view(request: HttpRequest) -> HttpResponse:
    cart = get_cart_for_request(request=request)
    removed_product_id = None
    if cart:
        removed_product_id = remove_cart_item(cart=cart, cart_item_id=int(request.POST.get("cart_item_id", 0)))
    
    triggers = {"cartUpdated": None}
    if removed_product_id:
        triggers["cartItemRemoved"] = {"product_id": removed_product_id}
        
    return _cart_page_response(request, hx_triggers=triggers)


@require_POST
def cart_quantity_view(request: HttpRequest) -> HttpResponse:
    """Increment/decrement a cart line's quantity (+1/-1); deletes at zero."""
    form = CartQuantityForm(request.POST)
    is_drawer = request.headers.get("HX-Target") == "cart-drawer-body"

    if not form.is_valid():
        if is_drawer:
            return _cart_drawer_response(request, hx_triggers={"cartUpdated": None})
        return _cart_page_response(request, error=_("Could not update quantity."))

    cart = get_cart_for_request(request=request)
    if cart is None:
        raise Http404("Cart not found.")

    try:
        adjust_cart_item_quantity(
            cart=cart,
            cart_item_id=form.cleaned_data["cart_item_id"],
            delta=form.cleaned_data["delta"],
        )
    except CartItemNotFoundError:
        if is_drawer:
            return _cart_drawer_response(request, hx_triggers={"cartUpdated": None})
        return _cart_page_response(request, error=_("That item is no longer in your cart."))
    except InsufficientStockError as exc:
        item_id = form.cleaned_data["cart_item_id"] if form.is_valid() else None
        if is_drawer:
            return _cart_drawer_response(request, error=str(exc), error_item_id=item_id)
        return _cart_page_response(request, error=str(exc), error_item_id=item_id)

    if is_drawer:
        return _cart_drawer_response(request, hx_triggers={"cartUpdated": None})
    return _cart_page_response(request, hx_triggers={"cartUpdated": None})


@require_POST
def cart_coupon_apply_view(request: HttpRequest) -> HttpResponse:
    """Validate and apply a coupon code to the cart."""
    form = CartCouponForm(request.POST)
    if not form.is_valid():
        return _cart_page_response(request, error=_("Enter a coupon code."))

    cart = get_cart_for_request(request=request)
    if cart is None:
        raise Http404("Cart not found.")

    try:
        apply_coupon(cart=cart, code=form.cleaned_data["code"])
    except InvalidCouponError as exc:
        return _cart_page_response(request, error=str(exc))

    return _cart_page_response(request, hx_triggers={"cartUpdated": None})


@require_POST
def cart_coupon_remove_view(request: HttpRequest) -> HttpResponse:
    """Remove any applied coupon from the cart."""
    cart = get_cart_for_request(request=request)
    if cart:
        remove_coupon(cart=cart)
    return _cart_page_response(request, hx_triggers={"cartUpdated": None})


@require_POST
def wishlist_toggle_view(request: HttpRequest) -> HttpResponse:
    """Toggle wishlist item; return JSON for HTMX or redirect back."""
    product_id = int(request.POST.get("product_id", 0))
    added = toggle_wishlist(request=request, product_id=product_id)
    count = get_wishlist_count(request=request)
    if request.headers.get("HX-Request"):
        response = HttpResponse(
            json.dumps(
                {
                    "status": "added" if added else "removed",
                    "product_id": product_id,
                    "added": added,
                    "count": count,
                }
            ),
            content_type="application/json",
        )
        response["HX-Trigger"] = json.dumps(
            {
                "wishlistUpdated": {
                    "added": added,
                    "product_id": product_id,
                    "count": count,
                }
            }
        )
        return response
    return redirect(request.META.get("HTTP_REFERER", "/"))
