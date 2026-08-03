"""Django admin registrations for the delivery app."""

from __future__ import annotations

from django.contrib import admin

from delivery.models import City, Country, DeliveryZone


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "code", "is_active", "updated_at")
    list_filter = ("is_active",)
    search_fields = ("name", "code")


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    """Admin interface for deliverable cities."""

    list_display = (
        "name",
        "slug",
        "country",
        "delivery_charge_base",
        "same_day_cutoff_hour",
        "is_active",
        "updated_at",
    )
    list_filter = ("is_active", "country")
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    ordering = ("name",)


@admin.register(DeliveryZone)
class DeliveryZoneAdmin(admin.ModelAdmin):
    list_display = ("name", "city", "radius_km", "is_active")
    list_filter = ("is_active", "city")



