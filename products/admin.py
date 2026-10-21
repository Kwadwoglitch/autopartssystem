
# ============================================================
# DJANGO AND PYTHON IMPORTS
# ============================================================

from decimal import Decimal

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import DecimalField, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path, reverse
from django.utils.html import format_html

# ============================================================
# PROJECT IMPORTS
# ============================================================


from .forms import (
    PaymentForm,
    StockAdjustmentForm,
    SaleReturnForm,
    RefundForm,
    SaleReturnItemForm,
)



from .models import (
    Brand,
    Category,
    VehicleModel,
    Supplier,
    Product,
    Purchase,
    PurchaseItem,
    InventoryTransaction,
    Customer,
    Sale,
    SaleItem,
    Payment,
    SaleReturn,
    SaleReturnItem,
    Refund,
    Expense,
)


from .services import (
    receive_purchase,
    complete_sale,
    record_payment,
    adjust_stock,
    process_sale_return,
    record_refund,
)


# ============================================================
# BRAND ADMIN
# ============================================================

@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


# ============================================================
# CATEGORY ADMIN
# ============================================================

@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at")
    search_fields = ("name",)


# ============================================================
# VEHICLE MODEL ADMIN
# ============================================================

@admin.register(VehicleModel)
class VehicleModelAdmin(admin.ModelAdmin):
    list_display = (
        "make",
        "model",
        "year_start",
        "year_end",
        "engine",
        "fuel_type",
        "transmission",
    )

    search_fields = (
        "make",
        "model",
        "engine",
        "fuel_type",
        "transmission",
    )

    list_filter = (
        "make",
        "fuel_type",
        "transmission",
    )


# ============================================================
# SUPPLIER ADMIN
# ============================================================

@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "contact_person",
        "phone",
        "email",
        "created_at",
    )

    search_fields = (
        "name",
        "contact_person",
        "phone",
        "email",
    )


# ============================================================
# PRODUCT ADMIN AND STOCK ADJUSTMENT
# ============================================================

@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "part_number",
        "brand",
        "category",
        "selling_price",
        "quantity",
        "adjust_stock_link",
    )

    search_fields = (
        "name",
        "part_number",
        "barcode",
        "brand__name",
        "category__name",
    )

    list_filter = (
        "brand",
        "category",
    )

    def get_urls(self):
        custom_urls = [
            path(
                "<int:product_id>/adjust-stock/",
                self.admin_site.admin_view(
                    self.adjust_stock_view
                ),
                name="products_product_adjust_stock",
            ),
        ]

        return custom_urls + super().get_urls()

    @admin.display(description="Stock adjustment")
    def adjust_stock_link(self, obj):
        url = reverse(
            "admin:products_product_adjust_stock",
            args=[obj.pk],
        )

        return format_html(
            '<a href="{}">Adjust stock</a>',
            url,
        )

    def adjust_stock_view(self, request, product_id):
        if not self.has_change_permission(request):
            raise PermissionDenied

        product = get_object_or_404(
            Product,
            pk=product_id,
        )

        if request.method == "POST":
            form = StockAdjustmentForm(request.POST)

            if form.is_valid():
                reason = form.cleaned_data["reason"].strip()

                try:
                    adjust_stock(
                        product_id=product.pk,
                        new_quantity=form.cleaned_data[
                            "new_quantity"
                        ],
                        reason=reason,
                        created_by=request.user,
                    )

                except ValueError as error:
                    form.add_error(None, str(error))

                except Product.DoesNotExist:
                    form.add_error(
                        None,
                        "This product no longer exists.",
                    )

                else:
                    self.message_user(
                        request,
                        (
                            f'Stock for "{product}" was adjusted '
                            "and the inventory transaction recorded."
                        ),
                        level=messages.SUCCESS,
                    )

                    return redirect(
                        reverse(
                            "admin:products_product_changelist"
                        )
                    )

        else:
            form = StockAdjustmentForm()

        context = {
            **self.admin_site.each_context(request),
            "opts": self.model._meta,
            "title": f"Adjust stock: {product}",
            "product": product,
            "form": form,
        }

        return render(
            request,
            "admin/products/product/adjust_stock.html",
            context,
        )


# ============================================================
# PURCHASE ITEMS INLINE
# ============================================================

class PurchaseItemInline(admin.TabularInline):
    model = PurchaseItem
    extra = 1


# ============================================================
# PURCHASE ADMIN
# ============================================================

@admin.register(Purchase)
class PurchaseAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "supplier",
        "invoice_number",
        "purchase_date",
        "status",
        "created_at",
    )

    search_fields = (
        "invoice_number",
        "supplier__name",
    )

    list_filter = (
        "status",
        "supplier",
        "purchase_date",
    )

    inlines = [PurchaseItemInline]
    actions = ["receive_selected_purchases"]

    @admin.action(description="Receive selected purchases")
    def receive_selected_purchases(self, request, queryset):
        received_count = 0
        error_count = 0

        for purchase in queryset:
            try:
                receive_purchase(
                    purchase,
                    received_by=request.user,
                )
                received_count += 1

            except (ValueError, ValidationError) as error:
                error_count += 1

                self.message_user(
                    request,
                    f"Purchase #{purchase.id}: {error}",
                    level=messages.ERROR,
                )

        if received_count:
            self.message_user(
                request,
                (
                    f"{received_count} purchase(s) "
                    "received successfully."
                ),
                level=messages.SUCCESS,
            )

        if error_count:
            self.message_user(
                request,
                (
                    f"{error_count} purchase(s) "
                    "could not be received."
                ),
                level=messages.WARNING,
            )


# ============================================================
# PURCHASE ITEM ADMIN
# ============================================================

@admin.register(PurchaseItem)
class PurchaseItemAdmin(admin.ModelAdmin):
    list_display = (
        "purchase",
        "product",
        "quantity",
        "unit_cost",
    )

    search_fields = (
        "product__name",
        "product__part_number",
        "purchase__invoice_number",
    )


# ============================================================
# INVENTORY TRANSACTION ADMIN
# READ-ONLY INVENTORY AUDIT HISTORY
# ============================================================

@admin.register(InventoryTransaction)
class InventoryTransactionAdmin(admin.ModelAdmin):
    list_display = (
        "product",
        "transaction_type",
        "quantity",
        "reference",
        "created_at",
        "created_by",
    )

    list_filter = (
        "transaction_type",
        "created_at",
    )

    search_fields = (
        "product__name",
        "reference",
        "notes",
        "created_by__username",
    )

    readonly_fields = (
        "product",
        "transaction_type",
        "quantity",
        "reference",
        "notes",
        "created_at",
        "created_by",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


# ============================================================
# CUSTOMER ADMIN
# ============================================================

@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "customer_type",
        "phone",
        "email",
        "created_at",
    )

    search_fields = (
        "name",
        "phone",
        "alternate_phone",
        "email",
    )

    list_filter = (
        "customer_type",
        "created_at",
    )

    readonly_fields = (
        "created_at",
        "updated_at",
    )


# ============================================================
# SALE ITEM INLINE
# ============================================================


class SaleItemInline(admin.TabularInline):
    model = SaleItem
    extra = 1

    def has_add_permission(self, request, obj=None):
        if obj and obj.status == "COMPLETED":
            return False

        return super().has_add_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == "COMPLETED":
            return False

        return super().has_delete_permission(request, obj)



# ============================================================
# PAYMENT HISTORY INLINE
# ============================================================

class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    can_delete = False

    fields = (
        "amount",
        "payment_method",
        "reference",
        "received_at",
        "notes",
        "created_by",
    )

    readonly_fields = (
        "amount",
        "payment_method",
        "reference",
        "received_at",
        "notes",
        "created_by",
    )

    def has_add_permission(self, request, obj=None):
        return False


# ============================================================
# SALE ADMIN
# ============================================================

@admin.register(Sale)
class SaleAdmin(admin.ModelAdmin):
    change_list_template = "admin/products/sale_changelist.html"


    def get_readonly_fields(self, request, obj=None):
        readonly_fields = list(
            super().get_readonly_fields(request, obj)
        )

        if obj and obj.status == "COMPLETED":
            model_fields = [
                field.name
                for field in self.model._meta.fields
            ]

            for field_name in model_fields:
                if field_name not in readonly_fields:
                    readonly_fields.append(field_name)

        return readonly_fields

    def has_delete_permission(self, request, obj=None):
        if obj and obj.status == "COMPLETED":
            return False

        return super().has_delete_permission(request, obj)


    list_display = (
        "invoice_number",
        "customer",
        "sale_date",
        "status",
        "total_amount_display",
        "amount_paid_display",
        "outstanding_balance_display",
        "payment_status",
    )

    search_fields = (
        "invoice_number",
        "customer__name",
        "customer__phone",
    )

    list_filter = (
        "status",
        "payment_status",
        "sale_date",
    )

    inlines = [
        SaleItemInline,
        PaymentInline,
    ]

    actions = ["complete_selected_sales"]

    def get_queryset(self, request):
        queryset = super().get_queryset(request)

        return queryset.annotate(
            admin_amount_paid=Coalesce(
                Sum("payments__amount"),
                Value(Decimal("0.00")),
                output_field=DecimalField(
                    max_digits=12,
                    decimal_places=2,
                ),
            )
        )


    def get_urls(self):
        custom_urls = [
            path(
                "outstanding-invoices/",
                self.admin_site.admin_view(
                    self.outstanding_invoices_view
                ),
                name="products_sale_outstanding",
            ),
            path(
                "record-return/",
                self.admin_site.admin_view(
                    self.record_return_view
                ),
                name="products_sale_record_return",
            ),
            path(
                "sale-return-items/",
                self.admin_site.admin_view(
                    self.sale_return_items_view
                ),
                name="products_sale_return_items",
            ),
            path(
                "record-refund/",
                self.admin_site.admin_view(
                    self.record_refund_view
                ),
                name="products_sale_record_refund",
            ),
        ]

        return custom_urls + super().get_urls()


    @admin.display(description="Invoice Total")
    def total_amount_display(self, obj):
        return obj.total_amount

    @admin.display(description="Amount Paid")
    def amount_paid_display(self, obj):
        return obj.admin_amount_paid

    @admin.display(description="Balance Due")
    def outstanding_balance_display(self, obj):
        return obj.total_amount - obj.admin_amount_paid

    @admin.action(description="Complete selected sales")
    def complete_selected_sales(self, request, queryset):
        completed_count = 0
        error_count = 0

        for sale in queryset:
            try:
                complete_sale(
                    sale,
                    completed_by=request.user,
                )
                completed_count += 1

            except (ValueError, ValidationError) as error:
                error_count += 1

                self.message_user(
                    request,
                    f"Sale {sale.invoice_number}: {error}",
                    level=messages.ERROR,
                )

        if completed_count:
            self.message_user(
                request,
                (
                    f"{completed_count} sale(s) "
                    "completed successfully."
                ),
                level=messages.SUCCESS,
            )

        if error_count:
            self.message_user(
                request,
                (
                    f"{error_count} sale(s) "
                    "could not be completed."
                ),
                level=messages.WARNING,
            )

    def outstanding_invoices_view(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied

        sales = (
            Sale.objects.filter(status="COMPLETED")
            .select_related("customer")
            .annotate(
                admin_amount_paid=Coalesce(
                    Sum("payments__amount"),
                    Value(Decimal("0.00")),
                    output_field=DecimalField(
                        max_digits=12,
                        decimal_places=2,
                    ),
                )
            )
            .order_by("-sale_date")
        )

        outstanding_sales = [
            sale
            for sale in sales
            if sale.total_amount > sale.admin_amount_paid
        ]

        search_query = request.GET.get("q", "").strip()

        if search_query:
            query = search_query.casefold()

            outstanding_sales = [
                sale
                for sale in outstanding_sales
                if (
                    query in sale.invoice_number.casefold()
                    or query in str(sale.customer).casefold()
                )
            ]

        outstanding_count = len(outstanding_sales)

        for sale in outstanding_sales:
            sale.admin_balance_due = (
                sale.total_amount - sale.admin_amount_paid
            )

        total_outstanding = sum(
            (
                sale.admin_balance_due
                for sale in outstanding_sales
            ),
            Decimal("0.00"),
        )

        context = {
            **self.admin_site.each_context(request),
            "title": "Outstanding Invoices",
            "outstanding_sales": outstanding_sales,
            "total_outstanding": total_outstanding,
            "outstanding_count": outstanding_count,
            "search_query": search_query,
            "opts": self.model._meta,
        }

        return render(
            request,
            "admin/products/outstanding_invoices.html",
            context,
        )




    def record_return_view(self, request):
        if not request.user.has_perm("products.add_salereturn"):
            raise PermissionDenied

        if request.method == "POST":
            form = SaleReturnForm(request.POST)

            sale = None
            if form.is_valid():
                sale = form.cleaned_data["sale"]

            item_form = SaleReturnItemForm(
                request.POST,
                sale=sale,
            )

            if form.is_valid() and item_form.is_valid():
                sale_item = item_form.cleaned_data["sale_item"]
                quantity = item_form.cleaned_data["quantity"]
                restock = item_form.cleaned_data["restock"]

                try:
                    sale_return = process_sale_return(
                        sale=sale,
                        items=[
                            {
                                "sale_item_id": sale_item.pk,
                                "quantity": quantity,
                                "restock": restock,
                            }
                        ],
                        reason=form.cleaned_data["reason"],
                        created_by=request.user,
                    )

                    self.message_user(
                        request,
                        f"{sale_return} recorded successfully.",
                        level=messages.SUCCESS,
                    )

                    return HttpResponseRedirect(
                        reverse("admin:products_salereturn_changelist")
                    )

                except (ValueError, ValidationError) as error:
                    form.add_error(None, str(error))

        else:
            form = SaleReturnForm()
            item_form = SaleReturnItemForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Record Sale Return",
            "form": form,
            "item_form": item_form,
            "opts": self.model._meta,
        }

        return render(
            request,
            "admin/products/sale_return_form.html",
            context,
        )



    def sale_return_items_view(self, request):
        if not request.user.has_perm("products.add_salereturn"):
            raise PermissionDenied

        sale_id = request.GET.get("sale_id")

        if not sale_id:
            return JsonResponse({"items": []})

        try:
            sale = Sale.objects.get(
                pk=sale_id,
                status="COMPLETED",
            )
        except (Sale.DoesNotExist, ValueError, TypeError):
            return JsonResponse(
                {"error": "Completed invoice not found."},
                status=404,
            )

        items = (
            SaleItem.objects.filter(sale=sale)
            .select_related("product")
            .order_by("pk")
        )

        return JsonResponse({
            "items": [
                {
                    "id": item.pk,
                    "label": (
                        f"{item.product.name} — "
                        f"Sold: {item.quantity} — "
                        f"Unit price: GH₵ {item.unit_price:.2f}"
                    ),
                }
                for item in items
            ]
        })



    def record_refund_view(self, request):
        if not request.user.has_perm("products.add_refund"):
            raise PermissionDenied

        if request.method == "POST":
            form = RefundForm(request.POST)

            if form.is_valid():
                try:
                    refund = record_refund(
                        sale_return=form.cleaned_data["sale_return"],
                        amount=form.cleaned_data["amount"],
                        refund_method=form.cleaned_data["refund_method"],
                        reference=form.cleaned_data["reference"],
                        notes=form.cleaned_data["notes"],
                        created_by=request.user,
                    )

                    self.message_user(
                        request,
                        f"Refund of GH₵ {refund.amount:.2f} recorded successfully.",
                        level=messages.SUCCESS,
                    )

                    return HttpResponseRedirect(
                        reverse("admin:products_refund_changelist")
                    )

                except (ValueError, ValidationError) as error:
                    form.add_error(None, str(error))
        else:
            form = RefundForm()

        context = {
            **self.admin_site.each_context(request),
            "title": "Record Customer Refund",
            "form": form,
            "opts": self.model._meta,
        }

        return render(
            request,
            "admin/products/refund_form.html",
            context,
        )


# ============================================================
# SALE ITEM ADMIN
# ============================================================

@admin.register(SaleItem)
class SaleItemAdmin(admin.ModelAdmin):
    list_display = (
        "sale",
        "product",
        "quantity",
        "unit_price",
    )

    search_fields = (
        "sale__invoice_number",
        "product__name",
        "product__part_number",
    )


# ============================================================
# PAYMENT ENTRY ADMIN
# ============================================================

class PaymentEntryAdmin(admin.ModelAdmin):
    def get_urls(self):
        custom_urls = [
            path(
                "record-payment/",
                self.admin_site.admin_view(
                    self.record_payment_view
                ),
                name="products_payment_record",
            ),
        ]

        return custom_urls + super().get_urls()

    def record_payment_view(self, request):
        sale_id = request.GET.get("sale_id")

        # Invoice summary requires permission to view
        # both the invoice and its payment history.
        if request.method == "GET" and sale_id:
            if not request.user.has_perm("products.view_sale"):
                raise PermissionDenied

            if not request.user.has_perm("products.view_payment"):
                raise PermissionDenied

            try:
                sale = Sale.objects.get(
                    pk=sale_id,
                    status="COMPLETED",
                )

            except (Sale.DoesNotExist, ValueError):
                return JsonResponse(
                    {"error": "Invoice not found."},
                    status=404,
                )

            total_paid = (
                sale.payments.aggregate(
                    total=Sum("amount")
                )["total"]
                or Decimal("0.00")
            )

            return JsonResponse(
                {
                    "invoice_number": sale.invoice_number,
                    "total": str(sale.total_amount),
                    "paid": str(total_paid),
                    "balance": str(
                        sale.total_amount - total_paid
                    ),
                }
            )

        # Payment entry requires Payment add permission.
        if not request.user.has_perm("products.add_payment"):
            raise PermissionDenied

        if request.method == "POST":
            form = PaymentForm(request.POST)

            if form.is_valid():
                try:
                    payment = record_payment(
                        sale=form.cleaned_data["sale"],
                        amount=form.cleaned_data["amount"],
                        payment_method=form.cleaned_data[
                            "payment_method"
                        ],
                        reference=form.cleaned_data["reference"],
                        notes=form.cleaned_data["notes"],
                        created_by=request.user,
                    )

                    self.message_user(
                        request,
                        (
                            f"Payment of {payment.amount} "
                            "recorded successfully."
                        ),
                        level=messages.SUCCESS,
                    )

                    return HttpResponseRedirect(
                        reverse(
                            "admin:products_payment_changelist"
                        )
                    )

                except (ValueError, ValidationError) as error:
                    form.add_error(None, str(error))

        else:
            initial = {}

            if sale_id:
                try:
                    selected_sale = Sale.objects.get(
                        pk=sale_id,
                        status="COMPLETED",
                    )
                    initial["sale"] = selected_sale.pk

                except (Sale.DoesNotExist, ValueError):
                    pass

            form = PaymentForm(initial=initial)

        context = {
            **self.admin_site.each_context(request),
            "title": "Record Payment",
            "form": form,
            "opts": self.model._meta,
        }

        return render(
            request,
            "admin/products/payment_form.html",
            context,
        )


# ============================================================
# PAYMENT ADMIN
# ============================================================

@admin.register(Payment)
class PaymentAdmin(PaymentEntryAdmin):
    change_list_template = "admin/products/payment_changelist.html"

    list_display = (
        "id",
        "sale",
        "amount",
        "payment_method",
        "reference",
        "received_at",
        "created_by",
    )

    search_fields = (
        "sale__invoice_number",
        "reference",
        "sale__customer__name",
        "created_by__username",
    )

    list_filter = (
        "payment_method",
        "received_at",
    )

    readonly_fields = ("received_at",)

    def has_add_permission(self, request):
        return False



# ============================================================
# EXPENSE ADMIN
# ============================================================

@admin.register(Expense)
class ExpenseAdmin(admin.ModelAdmin):
    list_display = (
        "expense_date",
        "category",
        "description",
        "amount",
        "payment_method",
        "created_by",
    )

    list_filter = (
        "category",
        "payment_method",
        "expense_date",
    )

    search_fields = (
        "description",
        "reference",
        "notes",
    )

    date_hierarchy = "expense_date"

    readonly_fields = (
        "created_at",
        "updated_at",
    )


# ============================================================
# SALE RETURN ADMIN
# ============================================================

@admin.register(SaleReturn)
class SaleReturnAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sale",
        "total_amount",
        "created_by",
        "created_at",
    )

    search_fields = (
        "sale__invoice_number",
        "sale__customer__name",
        "reason",
    )

    list_filter = ("created_at",)

    readonly_fields = (
        "sale",
        "reason",
        "created_by",
        "created_at",
    )

    def has_add_permission(self, request):
        return False


@admin.register(SaleReturnItem)
class SaleReturnItemAdmin(admin.ModelAdmin):
    list_display = (
        "sale_return",
        "sale_item",
        "quantity",
        "unit_price",
        "restock",
    )

    search_fields = (
        "sale_return__sale__invoice_number",
        "sale_item__product__name",
    )

    list_filter = ("restock",)

    def has_add_permission(self, request):
        return False


# ============================================================
# REFUND ADMIN
# ============================================================

@admin.register(Refund)
class RefundAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "sale_return",
        "amount",
        "refund_method",
        "reference",
        "created_by",
        "created_at",
    )

    search_fields = (
        "sale_return__sale__invoice_number",
        "reference",
        "created_by__username",
    )

    list_filter = (
        "refund_method",
        "created_at",
    )

    readonly_fields = (
        "sale_return",
        "amount",
        "refund_method",
        "reference",
        "notes",
        "created_by",
        "created_at",
    )

    def has_add_permission(self, request):
        return False
