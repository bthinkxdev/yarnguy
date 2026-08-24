"""Catalog management views: products, categories, occasions, brands, recipients, reviews."""

from __future__ import annotations

from django.contrib import messages
from django.db.models import F, Case, When, Value, IntegerField
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from catalog.models import Brand, Category, Product, Review, HomePageProduct, SizeChart
from core.models import Currency
from dashboard import forms
from dashboard.access import dashboard_required
from dashboard.views.base import (
    DashboardCreateView,
    DashboardDeleteView,
    DashboardListView,
    DashboardUpdateView,
)


class ProductListView(DashboardListView):
    model = Product
    template_name = "dashboard/catalog/product_list.html"
    nav_section = "products"
    url_basename = "product"
    singular_name = "Product"
    plural_name = "Products"
    search_fields = ["name", "sku"]
    select_related = ["category", "brand", "homepage_featured"]
    prefetch_related = ["images"]
    paginate_by = 20

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.GET.get("status", "")
        top_product_ids = None
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        elif status == "low":
            qs = qs.filter(stock_quantity__lte=F("low_stock_threshold"), stock_quantity__gt=0)
        elif status == "out":
            qs = qs.filter(stock_quantity=0)
        elif status == "top":
            from django.db.models import Sum
            from orders.models import OrderItem
            from orders.services import REVENUE_ORDER_STATUSES
            top_product_ids = list(
                OrderItem.objects.filter(order__order_status__in=REVENUE_ORDER_STATUSES)
                .values("product_id")
                .annotate(total_revenue=Sum(F("unit_price") * F("quantity")))
                .filter(total_revenue__gt=0)
                .order_by("-total_revenue")
                .values_list("product_id", flat=True)
            )
            if top_product_ids:
                qs = qs.filter(id__in=top_product_ids)
            else:
                qs = qs.none()

        category = self.request.GET.get("category", "")
        if category.isdigit():
            category_id = int(category)
            category_ids = [category_id]
            category_ids.extend(
                Category.objects.filter(parent_id=category_id).values_list("id", flat=True)
            )
            qs = qs.filter(category_id__in=category_ids)
            
        if status == "top" and top_product_ids:
            preserved_order = Case(*[When(pk=pk, then=Value(pos)) for pos, pk in enumerate(top_product_ids)], output_field=IntegerField())
            return qs.order_by(preserved_order)
            
        #sort pinned products to the top, toggled-off to the bottom
        qs = qs.order_by(
            Case(
                When(homepage_featured__is_shown=True, then=Value(1)),
                When(homepage_featured__is_shown=False, then=Value(-1)),
                default=Value(0),
                output_field=IntegerField()
            ).desc(),
            Case(
                When(homepage_featured__is_shown=True, then=F("homepage_featured__updated_at")),
                default=None
            ).desc(nulls_last=True),
            Case(
                When(homepage_featured__is_shown=False, then=F("homepage_featured__updated_at")),
                default=None
            ).asc(nulls_last=True),
            "-created_at"
        )
        return qs

    STATUS_FILTER_LABELS = {
        "active": "Active",
        "inactive": "Inactive",
        "low": "Low Stock",
        "out": "Out of Stock",
        "top": "Top Selling",
    }

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        default_currency = Currency.objects.filter(is_default=True).first()
        context["currency_symbol"] = default_currency.symbol if default_currency else ""
        context["categories"] = Category.objects.order_by("name")
        status_filter = self.request.GET.get("status", "")
        category_filter = self.request.GET.get("category", "")
        context["status_filter"] = status_filter
        context["category_filter"] = category_filter

        active_filters = []
        if status_filter:
            params = self.request.GET.copy()
            params.pop("status", None)
            active_filters.append({
                "label": self.STATUS_FILTER_LABELS.get(status_filter, status_filter),
                "clear_url": f"{self.request.path}?{params.urlencode()}" if params else self.request.path,
            })
        if category_filter:
            category = Category.objects.filter(pk=category_filter).first()
            params = self.request.GET.copy()
            params.pop("category", None)
            active_filters.append({
                "label": category.name if category else "Category",
                "clear_url": f"{self.request.path}?{params.urlencode()}" if params else self.request.path,
            })
        context["active_filters"] = active_filters
        return context


class ProductDeleteView(DashboardDeleteView):
    model = Product
    nav_section = "products"
    url_basename = "product"
    singular_name = "Product"


def _render_product_form(request, product, mode):
    if request.method == "POST":
        form = forms.ProductForm(request.POST, request.FILES, instance=product)
        variants = forms.ProductVariantFormSet(request.POST, instance=product, prefix="variants")
        images = forms.ProductImageFormSet(
            request.POST, request.FILES, instance=product, prefix="images"
        )
        specifications = forms.ProductSpecificationFormSet(
            request.POST, instance=product, prefix="specifications"
        )
        if (
            form.is_valid()
            and variants.is_valid()
            and images.is_valid()
            and specifications.is_valid()
        ):
            product = form.save()
            variants.instance = product
            variants.save()

            if product.variants.exists():
                from django.db.models import Sum
                first_variant = product.variants.order_by("id").first()
                total_stock = product.variants.aggregate(total=Sum("stock_quantity"))["total"] or 0
                
                product.stock_quantity = total_stock
                product.base_price = first_variant.base_price
                product.mrp = first_variant.mrp
                product.purchase_price = first_variant.purchase_price
                product.save(update_fields=["stock_quantity", "base_price", "mrp", "purchase_price"])

            images.instance = product
            images.save()
            specifications.instance = product
            specifications.save()
            messages.success(request, f"Product '{product.name}' saved successfully.")
            return redirect("dashboard:product-list")

    else:
        form = forms.ProductForm(instance=product)
        variants = forms.ProductVariantFormSet(instance=product, prefix="variants")
        images = forms.ProductImageFormSet(instance=product, prefix="images")
        specifications = forms.ProductSpecificationFormSet(
            instance=product, prefix="specifications"
        )

    for f in [
        form,
        *variants.forms,
        variants.empty_form,
        *images.forms,
        images.empty_form,
        *specifications.forms,
        specifications.empty_form,
    ]:
        _style(f)

    context = {
        "nav_section": "products",
        "page_title": f"{'Add' if mode == 'create' else 'Edit'} Product",
        "form": form,
        "variants": variants,
        "images": images,
        "specifications": specifications,
        "form_mode": mode,
        "product": product,
        "cancel_url": reverse("dashboard:product-list"),
    }
    
    from catalog.models import ProductVariant
    context["existing_variant_types"] = ProductVariant.objects.exclude(variant_type="").values_list("variant_type", flat=True).distinct()
    
    return render(request, "dashboard/catalog/product_form.html", context)


def _style(form):
    """Apply Bootstrap classes to a form's widgets (shared with generic mixin)."""
    for field in form.fields.values():
        widget = field.widget
        css = widget.attrs.get("class", "")
        name = widget.__class__.__name__.lower()
        if "checkbox" in name:
            widget.attrs["class"] = (css + " form-check-input").strip()
        elif "select" in name:
            widget.attrs["class"] = (css + " form-select").strip()
        elif "file" in name:
            widget.attrs["class"] = (css + " form-control").strip()
        else:
            widget.attrs["class"] = (css + " form-control").strip()


@dashboard_required
@require_http_methods(["GET", "POST"])
def product_create(request):
    return _render_product_form(request, None, "create")


@dashboard_required
@require_http_methods(["GET", "POST"])
def product_update(request, pk):
    product = get_object_or_404(Product, pk=pk)
    return _render_product_form(request, product, "edit")


@dashboard_required
@require_http_methods(["POST"])
def product_home_toggle(request, pk):
    product = get_object_or_404(Product, pk=pk)
    home_prod, created = HomePageProduct.objects.get_or_create(product=product)
    
    #toggle the state
    home_prod.is_shown = not home_prod.is_shown
    home_prod.save()
    
    if home_prod.is_shown:
        messages.success(request, f"{product.name} order set as first.")
    else:
        messages.info(request, f"{product.name} order set as last.")
        
    return redirect("dashboard:product-list")


class CategoryListView(DashboardListView):
    model = Category
    nav_section = "categories"
    url_basename = "category"
    singular_name = "Category"
    plural_name = "Categories"
    search_fields = ["name", "slug"]
    filter_by_active_status = True
    columns = [
        {"label": "Name", "name": "name"},
        {"label": "Slug", "name": "slug"},
        {"label": "Parent", "name": "parent.name"},
        {"label": "Order", "name": "display_order"},
        {"label": "Active", "name": "is_active", "type": "bool"},
    ]


class CategoryCreateView(DashboardCreateView):
    model = Category
    form_class = forms.CategoryForm
    nav_section = "categories"
    url_basename = "category"
    singular_name = "Category"


class CategoryUpdateView(DashboardUpdateView):
    model = Category
    form_class = forms.CategoryForm
    nav_section = "categories"
    url_basename = "category"
    singular_name = "Category"


class CategoryDeleteView(DashboardDeleteView):
    model = Category
    nav_section = "categories"
    url_basename = "category"
    singular_name = "Category"



class BrandListView(DashboardListView):
    model = Brand
    nav_section = "brands"
    url_basename = "brand"
    singular_name = "Brand"
    plural_name = "Brands"
    search_fields = ["name", "slug"]
    filter_by_featured_status = True
    columns = [
        {"label": "Name", "name": "name"},
        {"label": "Slug", "name": "slug"},
        {"label": "Featured", "name": "is_featured", "type": "bool"},
    ]


class BrandCreateView(DashboardCreateView):
    model = Brand
    form_class = forms.BrandForm
    nav_section = "brands"
    url_basename = "brand"
    singular_name = "Brand"


class BrandUpdateView(DashboardUpdateView):
    model = Brand
    form_class = forms.BrandForm
    nav_section = "brands"
    url_basename = "brand"
    singular_name = "Brand"


class BrandDeleteView(DashboardDeleteView):
    model = Brand
    nav_section = "brands"
    url_basename = "brand"
    singular_name = "Brand"



class SizeChartListView(DashboardListView):
    model = SizeChart
    nav_section = "size_charts"
    url_basename = "sizechart"
    singular_name = "Size Chart"
    plural_name = "Size Charts"
    search_fields = ["name"]
    filter_by_active_status = True
    columns = [
        {"label": "Name", "name": "name"},
        {"label": "Created At", "name": "created_at", "type": "datetime"},
        {"label": "Active", "name": "is_active", "type": "bool"},
    ]


class SizeChartCreateView(DashboardCreateView):
    model = SizeChart
    form_class = forms.SizeChartForm
    nav_section = "size_charts"
    url_basename = "sizechart"
    singular_name = "Size Chart"
    template_name = "dashboard/catalog/sizechart_form.html"


class SizeChartUpdateView(DashboardUpdateView):
    model = SizeChart
    form_class = forms.SizeChartForm
    nav_section = "size_charts"
    url_basename = "sizechart"
    singular_name = "Size Chart"
    template_name = "dashboard/catalog/sizechart_form.html"


class SizeChartDeleteView(DashboardDeleteView):
    model = SizeChart
    nav_section = "size_charts"
    url_basename = "sizechart"
    singular_name = "Size Chart"



class ReviewListView(DashboardListView):
    model = Review
    nav_section = "reviews"
    url_basename = "review"
    singular_name = "Review"
    plural_name = "Reviews"
    select_related = ["product", "customer__user"]
    can_create = False
    columns = [
        {"label": "Product", "name": "product.name"},
        {"label": "Rating", "name": "rating"},
        {"label": "Title", "name": "title"},
        {"label": "Status", "name": "get_moderation_status_display", "type": "badge"},
        {"label": "Submitted", "name": "created_at", "type": "datetime"},
    ]


class ReviewUpdateView(DashboardUpdateView):
    model = Review
    form_class = forms.ReviewForm
    nav_section = "reviews"
    url_basename = "review"
    singular_name = "Review"

    def form_valid(self, form):
        form.instance.moderated_by = self.request.user
        
        #clear notification 
        original_review = self.get_object()
        from notifications.models import Notification
        body_text = f'Review "{original_review.title}" on {original_review.product.name} awaits approval.'
        Notification.objects.filter(
            title="Review pending moderation", 
            body=body_text,
            is_read=False
        ).update(is_read=True)
        
        return super().form_valid(form)


class ReviewDeleteView(DashboardDeleteView):
    model = Review
    nav_section = "reviews"
    url_basename = "review"
    singular_name = "Review"

    def form_valid(self, form):
        #clear notification 
        original_review = self.get_object()
        from notifications.models import Notification
        body_text = f'Review "{original_review.title}" on {original_review.product.name} awaits approval.'
        Notification.objects.filter(
            title="Review pending moderation", 
            body=body_text,
            is_read=False
        ).update(is_read=True)
        
        return super().form_valid(form)
