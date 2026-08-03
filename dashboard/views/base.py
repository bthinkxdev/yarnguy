"""Reusable generic CRUD views for the admin dashboard.

These thin generic views keep every management screen consistent while letting
each entity declare only its columns, form, and URL naming. Reads stay in the
ORM/selectors, writes go through Django ``ModelForm`` (or app services where a
screen overrides ``form_valid``).
"""

from __future__ import annotations

from django.contrib import messages
from django.db.models import Q
from django.urls import reverse
from django.views.generic import CreateView, DeleteView, ListView, UpdateView

from dashboard.access import DashboardAccessMixin


class DashboardContextMixin(DashboardAccessMixin):
    """Common chrome context (title, active nav, breadcrumbs)."""

    nav_section: str = ""
    page_title: str = ""

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["nav_section"] = self.nav_section
        context["page_title"] = self.page_title
        return context


class DashboardListView(DashboardContextMixin, ListView):
    """Generic paginated, searchable list screen."""

    template_name = "dashboard/crud/list.html"
    paginate_by = 25
    context_object_name = "objects"

    columns: list[dict] = []
    search_fields: list[str] = []
    select_related: list[str] = []
    prefetch_related: list[str] = []
    url_basename: str = ""
    singular_name: str = ""
    plural_name: str = ""
    can_create: bool = True
    can_view: bool = False
    can_edit: bool = True
    can_delete: bool = True
    default_ordering: list[str] = ["-pk"]

    def get_queryset(self):
        qs = super().get_queryset()
        if self.select_related:
            qs = qs.select_related(*self.select_related)
        if self.prefetch_related:
            qs = qs.prefetch_related(*self.prefetch_related)
        query = self.request.GET.get("q", "").strip()
        if query and self.search_fields:
            filters = Q()
            for field in self.search_fields:
                filters |= Q(**{f"{field}__icontains": query})
            qs = qs.filter(filters)
        if not qs.ordered:
            qs = qs.order_by(*self.default_ordering)
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["columns"] = self.columns
        context["url_basename"] = self.url_basename
        context["singular_name"] = self.singular_name or self.model._meta.verbose_name.title()
        context["plural_name"] = self.plural_name or self.model._meta.verbose_name_plural.title()
        context["search_query"] = self.request.GET.get("q", "")
        context["searchable"] = bool(self.search_fields)
        context["can_create"] = self.can_create
        context["can_view"] = self.can_view
        context["can_edit"] = self.can_edit
        context["can_delete"] = self.can_delete
        if self.can_create and self.url_basename:
            context["create_url"] = reverse(f"dashboard:{self.url_basename}-create")
        params = self.request.GET.copy()
        params.pop("page", None)
        context["querystring"] = params.urlencode()
        return context


class _FormStyleMixin:
    """Apply Bootstrap classes to plain ModelForm widgets at render time."""

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        for field in form.fields.values():
            widget = field.widget
            css = widget.attrs.get("class", "")
            name = widget.__class__.__name__.lower()
            if "checkbox" in name:
                widget.attrs["class"] = (css + " form-check-input").strip()
            elif "select" in name:
                widget.attrs["class"] = (css + " form-select").strip()
            else:
                widget.attrs["class"] = (css + " form-control").strip()
        return form


class DashboardCreateView(_FormStyleMixin, DashboardContextMixin, CreateView):
    template_name = "dashboard/crud/form.html"
    url_basename = ""
    singular_name = ""

    def get_success_url(self):
        return reverse(f"dashboard:{self.url_basename}-list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.singular_name} created successfully.")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_mode"] = "create"
        context["singular_name"] = self.singular_name
        context["cancel_url"] = reverse(f"dashboard:{self.url_basename}-list")
        return context


class DashboardUpdateView(_FormStyleMixin, DashboardContextMixin, UpdateView):
    template_name = "dashboard/crud/form.html"
    url_basename = ""
    singular_name = ""

    def get_success_url(self):
        return reverse(f"dashboard:{self.url_basename}-list")

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"{self.singular_name} updated successfully.")
        return response

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["form_mode"] = "edit"
        context["singular_name"] = self.singular_name
        context["cancel_url"] = reverse(f"dashboard:{self.url_basename}-list")
        return context


class DashboardDeleteView(DashboardContextMixin, DeleteView):
    template_name = "dashboard/crud/confirm_delete.html"
    url_basename = ""
    singular_name = ""

    def get_success_url(self):
        return reverse(f"dashboard:{self.url_basename}-list")

    def form_valid(self, form):
        messages.success(self.request, f"{self.singular_name} deleted.")
        return super().form_valid(form)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["singular_name"] = self.singular_name
        context["cancel_url"] = reverse(f"dashboard:{self.url_basename}-list")
        return context
