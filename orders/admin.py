"""Django admin registrations for the orders app."""

from __future__ import annotations

from django.contrib import admin

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
        from indiapost.services import trigger_indiapost_shipment
        from django.contrib import messages
        import logging
        admin_logger = logging.getLogger(__name__)

        success_count = 0
        fail_count = 0
        for order in queryset:
            if order.order_status in (OrderStatus.CANCELLED, OrderStatus.REFUNDED):
                self.message_user(
                    request,
                    f"Skipped Order #{order.order_number}: Cannot book {order.get_order_status_display()} order.",
                    level=messages.WARNING,
                )
                continue
            try:
                if trigger_indiapost_shipment(order, force=True):
                    success_count += 1
                else:
                    fail_count += 1
            except Exception as e:
                admin_logger.error(f"Error booking order {order.order_number} with India Post: {e}", exc_info=True)
                fail_count += 1
        self.message_user(
            request,
            f"India Post booking result: {success_count} succeeded, {fail_count} failed."
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
