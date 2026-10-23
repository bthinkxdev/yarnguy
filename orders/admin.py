"""Django admin registrations for the orders app."""

from __future__ import annotations

from django.contrib import admin, messages

from orders.models import Order, OrderItem, OrderStatusHistory, ProofOfDelivery
from orders.services import transition_order_status


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


class OrderStatusHistoryInline(admin.TabularInline):
    model = OrderStatusHistory
    extra = 0
    readonly_fields = ("from_status", "to_status", "changed_by", "changed_at", "note")
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = (
        "order_number",
        "customer_profile",
        "order_status",
        "delivery_partner",
        "total_amount",
        "created_at",
    )
    list_filter = ("order_status", "delivery_partner")
    inlines = [OrderItemInline, OrderStatusHistoryInline]

    actions = ["book_with_india_post"]

    @admin.action(description="Book selected orders with India Post")
    def book_with_india_post(self, request, queryset):
        """
        Idempotent: an order that is already booked is reported as such and nothing is
        re-sent to India Post. A failed order is retried, reusing its reserved barcode.
        """
        from indiapost.services import IndiaPostBookingError, book_shipment

        booked = already = failed = 0
        for order in queryset.select_related("customer_profile__user"):
            had_barcode_before = hasattr(order, "indiapost_shipment") and order.indiapost_shipment.is_booked
            try:
                book_shipment(order)
            except IndiaPostBookingError as exc:
                failed += 1
                self.message_user(request, f"Order {order.order_number}: {exc}", level=messages.ERROR)
                continue
            if had_barcode_before:
                already += 1
            else:
                booked += 1
        self.message_user(
            request,
            f"India Post: {booked} booked, {already} already booked, {failed} failed.",
            level=messages.WARNING if failed else messages.SUCCESS,
        )

    def save_model(self, request, obj, form, change) -> None:
        if change:
            existing = Order.objects.filter(pk=obj.pk).first()
            if existing and existing.order_status != obj.order_status:
                transition_order_status(
                    order=existing,
                    new_status=obj.order_status,
                    actor=request.user,
                    note="Updated via Django admin.",
                )
                return
        super().save_model(request, obj, form, change)


@admin.register(ProofOfDelivery)
class ProofOfDeliveryAdmin(admin.ModelAdmin):
    list_display = ("order", "recipient_name", "delivered_at")
