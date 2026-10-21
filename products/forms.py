#======================================================
# LIBRARY IMPORTATIONS
#======================================================

from decimal import Decimal

from django import forms
from django.db.models import Sum

from .models import (
    Sale,
    SaleItem,
    SaleReturnItem,
    SaleReturn,
    Refund,
    Payment,
)

class PaymentForm(forms.Form):
    sale = forms.ModelChoiceField(
        queryset=Sale.objects.none(),
        label='Invoice',
    )

    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal('0.01'),
        label='Payment amount',
    )

    payment_method = forms.ChoiceField(
        choices=Payment.PAYMENT_METHOD_CHOICES,
        label='Payment method',
    )

    reference = forms.CharField(
        max_length=100,
        required=False,
        label='Transaction reference',
    )

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={'rows': 3}),
        label='Notes',
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields['sale'].queryset = Sale.objects.filter(
            status='COMPLETED'
        ).order_by('-sale_date')

        self.fields['sale'].label_from_instance = (
            self.invoice_label
        )

    def invoice_label(self, sale):
        total_paid = sale.payments.aggregate(
            total=Sum('amount')
        )['total'] or Decimal('0.00')

        invoice_total = sale.total_amount
        balance = invoice_total - total_paid

        return (
            f'{sale.invoice_number} | '
            f'Total: {invoice_total:.2f} | '
            f'Paid: {total_paid:.2f} | '
            f'Balance: {balance:.2f}'
        )


class StockAdjustmentForm(forms.Form):
    new_quantity = forms.IntegerField(
        min_value=0,
        label='New physical stock quantity',
        help_text='Enter the actual quantity counted in the store.',
    )

    reason = forms.CharField(
        max_length=500,
        label='Reason for adjustment',
        widget=forms.Textarea(attrs={'rows': 3}),
        help_text=(
            'Explain why the recorded stock differs '
            'from the physical count.'
        ),
    )

    def clean_reason(self):
        reason = self.cleaned_data['reason'].strip()

        if not reason:
            raise forms.ValidationError(
                'A reason is required for every stock adjustment.'
            )

        return reason



class SaleReturnForm(forms.Form):
    sale = forms.ModelChoiceField(
        queryset=Sale.objects.none(),
        label="Completed invoice",
    )

    reason = forms.CharField(
        max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Reason for return",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["sale"].queryset = Sale.objects.filter(
            status="COMPLETED"
        ).order_by("-sale_date")

        self.fields["sale"].label_from_instance = (
            lambda sale: sale.invoice_number
        )

    def clean_reason(self):
        reason = self.cleaned_data["reason"].strip()

        if not reason:
            raise forms.ValidationError(
                "A reason is required for every return."
            )

        return reason


class RefundForm(forms.Form):
    sale_return = forms.ModelChoiceField(
        queryset=SaleReturn.objects.none(),
        label="Accepted return",
    )

    amount = forms.DecimalField(
        max_digits=10,
        decimal_places=2,
        min_value=Decimal("0.01"),
        label="Refund amount (GH₵)",
    )

    refund_method = forms.ChoiceField(
        choices=Refund.REFUND_METHOD_CHOICES,
        label="Refund method",
    )

    reference = forms.CharField(
        max_length=100,
        required=False,
        label="Transaction reference",
    )

    notes = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
        label="Notes",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.fields["sale_return"].queryset = (
            SaleReturn.objects.filter(
                sale__status="COMPLETED"
            ).select_related("sale").order_by("-created_at")
        )

        self.fields["sale_return"].label_from_instance = (
            lambda sale_return: (
                f"{sale_return} | "
                f"Value: GH₵ {sale_return.total_amount:.2f}"
            )
        )




class SaleReturnItemForm(forms.Form):
    sale_item = forms.ModelChoiceField(
        queryset=SaleItem.objects.none(),
        label="Original sale item",
    )

    quantity = forms.IntegerField(
        min_value=1,
        label="Quantity being returned",
    )

    restock = forms.BooleanField(
        required=False,
        initial=False,
        label="Approved for restocking",
        help_text=(
            "Tick only if the returned item is suitable "
            "for resale and should increase inventory."
        ),
    )

    def __init__(self, *args, sale=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.sale = sale

        if sale is not None:
            self.fields["sale_item"].queryset = (
                SaleItem.objects.filter(
                    sale=sale
                ).select_related("product").order_by("pk")
            )

            self.fields["sale_item"].label_from_instance = (
                lambda item: (
                    f"{item.product.name} — "
                    f"Sold: {item.quantity} — "
                    f"Unit price: GH₵ {item.unit_price:.2f}"
                )
            )

    def clean(self):
        cleaned_data = super().clean()
        sale_item = cleaned_data.get("sale_item")
        quantity = cleaned_data.get("quantity")

        if sale_item is not None and self.sale is not None:
            if sale_item.sale_id != self.sale.pk:
                self.add_error(
                    "sale_item",
                    "Select an item from the chosen invoice.",
                )

        if (
            sale_item is not None
            and quantity is not None
            and quantity > sale_item.quantity
        ):
            self.add_error(
                "quantity",
                "The return quantity cannot exceed the quantity originally sold.",
            )

        return cleaned_data
