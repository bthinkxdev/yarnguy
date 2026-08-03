"""Delivery configuration: cities and delivery slots."""

from __future__ import annotations

from dashboard import forms
from dashboard.views.base import (
    DashboardCreateView,
    DashboardDeleteView,
    DashboardListView,
    DashboardUpdateView,
)
from delivery.models import City


class CityListView(DashboardListView):
    model = City
    nav_section = "cities"
    url_basename = "city"
    singular_name = "City"
    plural_name = "Cities"
    search_fields = ["name", "slug"]
    select_related = ["country"]
    columns = [
        {"label": "City", "name": "name"},
        {"label": "Country", "name": "country.name"},
        {"label": "Delivery charge", "name": "delivery_charge_base", "type": "money"},
        {"label": "Same-day cutoff", "name": "same_day_cutoff_hour"},
        {"label": "Active", "name": "is_active", "type": "bool"},
    ]


class CityCreateView(DashboardCreateView):
    model = City
    form_class = forms.CityForm
    nav_section = "cities"
    url_basename = "city"
    singular_name = "City"


class CityUpdateView(DashboardUpdateView):
    model = City
    form_class = forms.CityForm
    nav_section = "cities"
    url_basename = "city"
    singular_name = "City"


class CityDeleteView(DashboardDeleteView):
    model = City
    nav_section = "cities"
    url_basename = "city"
    singular_name = "City"



