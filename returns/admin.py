from django.contrib import admin

from returns.models import ReturnItem, ReturnRequest


class ReturnItemInline(admin.TabularInline):
    model = ReturnItem
    extra = 0
    readonly_fields = ("order_item", "quantity", "reason", "restock")
    can_delete = False

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(ReturnRequest)
class ReturnRequestAdmin(admin.ModelAdmin):
    list_display = ("return_number", "order", "status", "carrier", "reverse_tracking_number", "created_at")
    list_filter = ("status", "carrier")
    search_fields = ("return_number", "order__order_number", "reverse_tracking_number")
    readonly_fields = ("pickup_response", "created_at", "updated_at")
    inlines = [ReturnItemInline]
