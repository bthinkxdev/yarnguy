"""Catalog management views: products, categories, occasions, brands, recipients, reviews."""

from __future__ import annotations

from django.contrib import messages
from django.db.models import F
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from catalog.models import Brand, Category, Product, Review
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
    select_related = ["category", "brand"]
    prefetch_related = ["images"]
    paginate_by = 20

    def get_queryset(self):
        qs = super().get_queryset()
        status = self.request.GET.get("status", "")
        if status == "active":
            qs = qs.filter(is_active=True)
        elif status == "inactive":
            qs = qs.filter(is_active=False)
        elif status == "low":
            qs = qs.filter(stock_quantity__lte=F("low_stock_threshold"))
        elif status == "out":
            qs = qs.filter(stock_quantity=0)
        category = self.request.GET.get("category", "")
        if category.isdigit():
            qs = qs.filter(category_id=int(category))
        return qs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        default_currency = Currency.objects.filter(is_default=True).first()
        context["currency_symbol"] = default_currency.symbol if default_currency else ""
        context["categories"] = Category.objects.order_by("name")
        context["status_filter"] = self.request.GET.get("status", "")
        context["category_filter"] = self.request.GET.get("category", "")
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
        documents = forms.ProductDocumentFormSet(
            request.POST, request.FILES, instance=product, prefix="documents"
        )
        if (
            form.is_valid()
            and variants.is_valid()
            and images.is_valid()
            and specifications.is_valid()
            and documents.is_valid()
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
            documents.instance = product
            documents.save()
            messages.success(request, f"Product '{product.name}' saved successfully.")
            return redirect("dashboard:product-list")

    else:
        form = forms.ProductForm(instance=product)
        variants = forms.ProductVariantFormSet(instance=product, prefix="variants")
        images = forms.ProductImageFormSet(instance=product, prefix="images")
        specifications = forms.ProductSpecificationFormSet(
            instance=product, prefix="specifications"
        )
        documents = forms.ProductDocumentFormSet(instance=product, prefix="documents")

    for f in [
        form,
        *variants.forms,
        variants.empty_form,
        *images.forms,
        images.empty_form,
        *specifications.forms,
        specifications.empty_form,
        *documents.forms,
        documents.empty_form,
    ]:
        _style(f)

    context = {
        "nav_section": "products",
        "page_title": f"{'Add' if mode == 'create' else 'Edit'} Product",
        "form": form,
        "variants": variants,
        "images": images,
        "specifications": specifications,
        "documents": documents,
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


class CategoryListView(DashboardListView):
    model = Category
    nav_section = "categories"
    url_basename = "category"
    singular_name = "Category"
    plural_name = "Categories"
    search_fields = ["name", "slug"]
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
