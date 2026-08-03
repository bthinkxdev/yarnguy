"""HTTP views for the checkout app."""

from __future__ import annotations

import re

from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from accounts.selectors import get_address_by_id, get_saved_addresses
from cart.selectors import get_cart_for_request, get_cart_summary
from cart.services import get_or_create_cart
from checkout.forms import CheckoutAddressForm, CheckoutDeliveryForm, CheckoutPaymentForm
from checkout.selectors import get_checkout_session_by_id
from checkout.services import create_checkout_session, place_order, update_checkout_session

from payments.registry import PAYMENT_GATEWAYS
from payments.services import process_payment


@require_GET
def checkout_view(request: HttpRequest) -> HttpResponse:
    """Multi-step checkout page with gift Order Preview partial."""
    cart = get_or_create_cart(request=request)
    summary = get_cart_summary(cart=cart)
    if not summary.lines:
        #do not redirect,render the empty state in checkout.html
        pass

    if request.user.is_authenticated:
        from accounts.services import ensure_customer_profile_for_user
        profile = ensure_customer_profile_for_user(user=request.user)
    else:
        profile = None

    session = create_checkout_session(
        cart=cart,
        customer_profile=profile,
        session_key=request.session.session_key or "",
    )


    addresses = []
    if profile:
        from accounts.models import Address
        dashboard_address = profile.default_address
        if not dashboard_address:
            dashboard_address = Address.objects.filter(customer_profile=profile).first()
        if dashboard_address:
            addresses = [dashboard_address]
            
    from delivery.models import City
    active_cities = City.objects.filter(is_active=True)

    selected_gateway_key = None
    if session.order:
        last_tx = session.order.payment_transactions.last()
        if last_tx:
            selected_gateway_key = last_tx.gateway_key

    from payments.adapters.concrete import _get_razorpay_credentials
    razorpay_key, razorpay_secret = _get_razorpay_credentials()
    
    available_gateways = {}
    for key, adapter in PAYMENT_GATEWAYS.items():
        if key.startswith("razorpay") and (not razorpay_key or not razorpay_secret):
            continue
        available_gateways[key] = adapter

    from marketing.selectors import has_any_active_coupons
    return render(
        request,
        "checkout/checkout.html",
        {
            "cart": cart,
            "summary": summary,
            "checkout_session": session,
            "addresses": addresses,
            "active_cities": active_cities,

            "payment_gateways": available_gateways,
            "selected_gateway_key": selected_gateway_key,
            "has_active_coupons": has_any_active_coupons(),
        },
    )


@require_http_methods(["POST"])
def checkout_place_order_view(request: HttpRequest) -> HttpResponse:
    """Place order and process payment in one HTMX step."""
    form = CheckoutPaymentForm(request.POST)
    if not form.is_valid():
        return render(
            request,
            "checkout/partials/errors.html",
            {"errors": form.errors},
            status=200,
        )

    cart = get_cart_for_request(request=request)
    if cart is None:
        raise Http404("Cart not found.")

    if request.user.is_authenticated:
        from accounts.services import ensure_customer_profile_for_user
        profile = ensure_customer_profile_for_user(user=request.user)
    else:
        profile = None

    session = create_checkout_session(cart=cart, customer_profile=profile, session_key=request.session.session_key or "")
    
    address = None
    address_form = CheckoutAddressForm(request.POST)
    if address_form.is_valid() and address_form.cleaned_data.get("address_id") and profile:
        address = get_address_by_id(
            address_id=address_form.cleaned_data["address_id"],
            customer_profile=profile,
        )
        if address:
            update_checkout_session(checkout_session=session, address=address)

    if not address:
        if not request.user.is_authenticated:
            guest_name = request.POST.get("guest_name", "").strip()
            guest_email = request.POST.get("guest_email", "").strip()
            guest_phone = request.POST.get("guest_phone", "").strip()
            guest_address_line1 = request.POST.get("guest_address_line1", "").strip()
            guest_address_line2 = request.POST.get("guest_address_line2", "").strip()
            guest_city = request.POST.get("guest_city", "").strip()

            errors = {}
            if not guest_name: 
                errors["guest_name"] = ["Name is required."]
            
            if not guest_email: 
                errors["guest_email"] = ["Email is required."]
            else:
                try:
                    validate_email(guest_email)
                except ValidationError:
                    errors["guest_email"] = ["Enter a valid email address."]

            if not guest_phone: 
                errors["guest_phone"] = ["Phone is required."]
            else:
                if not re.match(r'^\+?\d+$', guest_phone):
                    errors["guest_phone"] = ["Phone number can only contain numbers."]
                elif not re.match(r'^\+?\d{10,15}$', guest_phone):
                    errors["guest_phone"] = ["Enter a valid phone number."]

            if not guest_address_line1: errors["guest_address_line1"] = ["Address Line 1 is required."]
            if not guest_city: errors["guest_city"] = ["City is required."]

            if errors:
                return render(
                    request,
                    "checkout/partials/errors.html",
                    {"errors": errors},
                    status=200,
                )

            from accounts.services import login_or_create_customer_by_email
            from accounts.models import Address

            profile = login_or_create_customer_by_email(email=guest_email, name=guest_name)
            if guest_phone:
                profile.phone = guest_phone
                profile.save(update_fields=["phone", "updated_at"])

            address = Address.objects.filter(
                customer_profile=profile,
                line1=guest_address_line1,
                line2=guest_address_line2,
                city=guest_city,
            ).first()
            if not address:
                address = Address.objects.create(
                    customer_profile=profile,
                    line1=guest_address_line1,
                    line2=guest_address_line2,
                    city=guest_city,
                    label="Delivery Address"
                )
                
            if profile.default_address is None:
                from accounts.services import set_default_address
                set_default_address(customer_profile=profile, address_id=address.pk)
                
            update_checkout_session(checkout_session=session, address=address)
            
            #update session customer profile
            session.customer_profile = profile
            session.save(update_fields=["customer_profile", "updated_at"])
        else:
            guest_address_line1 = request.POST.get("guest_address_line1", "").strip()
            guest_address_line2 = request.POST.get("guest_address_line2", "").strip()
            guest_city = request.POST.get("guest_city", "").strip()

            errors = {}
            if not guest_address_line1: errors["guest_address_line1"] = ["Address Line 1 is required."]
            if not guest_city: errors["guest_city"] = ["City is required."]

            if errors:
                return render(
                    request,
                    "checkout/partials/errors.html",
                    {"errors": errors},
                    status=200,
                )

            from accounts.models import Address
            address = Address.objects.filter(
                customer_profile=profile,
                line1=guest_address_line1,
                line2=guest_address_line2,
                city=guest_city,
            ).first()
            if not address:
                address = Address.objects.create(
                    customer_profile=profile,
                    line1=guest_address_line1,
                    line2=guest_address_line2,
                    city=guest_city,
                    label="Delivery Address"
                )
            
            if profile.default_address is None:
                from accounts.services import set_default_address
                set_default_address(customer_profile=profile, address_id=address.pk)
                
            update_checkout_session(checkout_session=session, address=address)

    delivery_form = CheckoutDeliveryForm(request.POST)
    if delivery_form.is_valid():
        update_checkout_session(
            checkout_session=session,
            delivery_date=delivery_form.cleaned_data.get("delivery_date"),
        )

    from cart.selectors import get_cart_summary
    summary = get_cart_summary(cart=cart)
    if summary.has_stock_issues:
        from django.utils.translation import gettext as _
        return render(
            request,
            "checkout/partials/errors.html",
            {"errors": {"__all__": [_("Some items in your cart are out of stock. Please remove them to proceed.")]}},
            status=200,
        )

    try:
        from catalog.exceptions import InsufficientStockError
        order = place_order(
            checkout_session_id=session.pk,
            idempotency_key=form.cleaned_data["idempotency_key"],
            customer_profile=profile,
        )
    except InsufficientStockError as exc:
        return render(
            request,
            "checkout/partials/errors.html",
            {"errors": {"__all__": [str(exc)]}},
            status=200,
        )

    payment_data = {}

    gateway_key = form.cleaned_data["gateway_key"]
    process_payment(
        order=order,
        gateway_key=gateway_key,
        payment_data=payment_data,
    )

    if gateway_key.startswith("razorpay"):
        pay_url = reverse("checkout:razorpay-pay", kwargs={"order_id": order.pk})
        if request.headers.get("HX-Request"):
            response = HttpResponse()
            response["HX-Redirect"] = pay_url
            return response
        return redirect(pay_url)

    confirmation_url = reverse("checkout:confirmation", kwargs={"order_id": order.pk})
    if request.headers.get("HX-Request"):
        response = HttpResponse()
        response["HX-Redirect"] = confirmation_url
        return response
    return redirect(confirmation_url)


@require_GET
def checkout_confirmation_view(request: HttpRequest, order_id: int) -> HttpResponse:
    """Separate order confirmation / success page."""
    from orders.models import Order
    from django.shortcuts import get_object_or_404
    order = get_object_or_404(Order, pk=order_id)
    return render(
        request,
        "checkout/confirmation_page.html",
        {
            "order": order,
        },
    )


@require_GET
def razorpay_pay_view(request: HttpRequest, order_id: int) -> HttpResponse:
    """Render Razorpay checkout payment page."""
    from orders.models import Order
    from payments.models import PaymentTransaction
    from payments.adapters.concrete import _get_razorpay_credentials
    from django.shortcuts import get_object_or_404

    order = get_object_or_404(Order, pk=order_id)
    payment_tx = PaymentTransaction.objects.filter(order=order, gateway_key__startswith="razorpay").last()
    key_id, _ = _get_razorpay_credentials()

    customer_name = ""
    customer_email = ""
    customer_phone = ""
    if order.customer_profile:
        customer_name = f"{order.customer_profile.user.first_name} {order.customer_profile.user.last_name}".strip() or order.customer_profile.user.username
        customer_email = order.customer_profile.user.email
        customer_phone = order.customer_profile.phone
    elif order.delivery_address_snapshot:
        customer_name = order.delivery_address_snapshot.get("recipient_name", "")
        customer_email = order.delivery_address_snapshot.get("email", "")
        customer_phone = order.delivery_address_snapshot.get("phone", "")

    amount_in_paise = int(order.total_amount * 100)
    from core.selectors import get_default_currency
    default_curr = get_default_currency()
    currency_code = order.currency.code if order.currency else (default_curr.code if default_curr else "AED")
    razorpay_order_id = payment_tx.external_intent_id if payment_tx else f"rzp_order_{order.pk}"

    #determine prefill method based on gateway key
    prefill_method = ""
    payment_method_name = "Razorpay"
    if payment_tx and payment_tx.gateway_key:
        if payment_tx.gateway_key == "razorpay_upi":
            prefill_method = "upi"
            payment_method_name = "UPI"
        elif payment_tx.gateway_key == "razorpay_card":
            prefill_method = "card"
            payment_method_name = "Credit/Debit Card"
        elif payment_tx.gateway_key == "razorpay_netbanking":
            prefill_method = "netbanking"
            payment_method_name = "Net Banking"
        elif payment_tx.gateway_key == "razorpay_wallet":
            prefill_method = "wallet"
            payment_method_name = "Wallet"

    #store order_id in session so the callback can retrieve it
    request.session["razorpay_order_pk"] = order.pk

    #build absolute callback URL for Razorpay redirect
    callback_url = request.build_absolute_uri(reverse("checkout:razorpay-callback"))
    cancel_url = request.build_absolute_uri(reverse("checkout:checkout"))

    return render(
        request,
        "checkout/razorpay_pay.html",
        {
            "order": order,
            "razorpay_key_id": key_id or "rzp_test_mock",
            "razorpay_order_id": razorpay_order_id,
            "amount_in_paise": amount_in_paise,
            "currency_code": currency_code,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "callback_url": callback_url,
            "cancel_url": cancel_url,
            "prefill_method": prefill_method,
            "payment_method_name": payment_method_name,
        },
    )


from django.views.decorators.csrf import csrf_exempt


@csrf_exempt
@require_http_methods(["POST"])
def razorpay_callback_view(request: HttpRequest) -> HttpResponse:
    """
    Handle POST callback from Razorpay after payment.

    Razorpay redirects the full browser here with razorpay_payment_id,
    razorpay_order_id, and razorpay_signature as POST parameters.
    """
    from payments.models import PaymentTransaction
    from payments.adapters.concrete import RazorpayAdapter
    from payments.services import confirm_payment_success, confirm_payment_failed
    from django.shortcuts import get_object_or_404
    from orders.models import Order

    razorpay_payment_id = request.POST.get("razorpay_payment_id", "")
    razorpay_order_id = request.POST.get("razorpay_order_id", "")
    razorpay_signature = request.POST.get("razorpay_signature", "")

    #try order_id from POST (JS form submit) or session (Razorpay redirect)
    order_id = request.POST.get("order_id") or request.session.get("razorpay_order_pk")

    if not order_id:
        #fallback: look up order via Razorpay order ID stored in PaymentTransaction
        payment_tx = PaymentTransaction.objects.filter(
            external_intent_id=razorpay_order_id,
            gateway_key__startswith="razorpay",
        ).last()
        if payment_tx:
            order_id = payment_tx.order_id
        else:
            return redirect("checkout:checkout")

    order = get_object_or_404(Order, pk=order_id)
    payment_tx = PaymentTransaction.objects.filter(order=order, gateway_key__startswith="razorpay").last()

    #clean up session
    request.session.pop("razorpay_order_pk", None)

    adapter = RazorpayAdapter()
    is_valid = adapter.verify_payment_signature(
        razorpay_order_id=razorpay_order_id,
        razorpay_payment_id=razorpay_payment_id,
        razorpay_signature=razorpay_signature,
    )

    if is_valid and payment_tx:
        adapter.capture_payment(
            razorpay_payment_id=razorpay_payment_id,
            amount=payment_tx.amount,
            currency=payment_tx.currency.code if payment_tx.currency else (default_curr.code if default_curr else "AED"),
        )
        payment_tx.external_transaction_id = razorpay_payment_id
        payment_tx.save(update_fields=["external_transaction_id", "updated_at"])
        confirm_payment_success(payment_transaction=payment_tx)
        return redirect("checkout:confirmation", order_id=order.pk)
    else:
        if payment_tx:
            confirm_payment_failed(payment_transaction=payment_tx)
        return redirect("checkout:checkout")


@require_POST
def checkout_coupon_apply_view(request: HttpRequest) -> HttpResponse:
    """Validate and apply a coupon code from checkout."""
    from cart.forms import CartCouponForm
    from cart.services import apply_coupon
    from marketing.exceptions import InvalidCouponError
    from django.contrib import messages
    from cart.selectors import get_cart_for_request
    from django.utils.translation import gettext as _

    form = CartCouponForm(request.POST)
    if not form.is_valid():
        messages.error(request, _("Enter a valid coupon code."))
        return redirect("checkout:checkout")

    cart = get_cart_for_request(request=request)
    if cart is None:
        raise Http404("Cart not found.")

    try:
        apply_coupon(cart=cart, code=form.cleaned_data["code"])
        messages.success(request, _("Coupon applied successfully!"))
    except InvalidCouponError as exc:
        messages.error(request, str(exc))

    return redirect("checkout:checkout")


@require_POST
def checkout_coupon_remove_view(request: HttpRequest) -> HttpResponse:
    """Remove any applied coupon from the cart from checkout."""
    from cart.services import remove_coupon
    from cart.selectors import get_cart_for_request
    from django.contrib import messages
    from django.utils.translation import gettext as _
    
    cart = get_cart_for_request(request=request)
    if cart:
        remove_coupon(cart=cart)
        messages.success(request, _("Coupon removed."))
    return redirect("checkout:checkout")
