from decimal import Decimal
from django.db import models
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Sum
from django.shortcuts import render
from django.utils import timezone
from .models import Product, Sale, Payment
from datetime import date
from .reporting import profit_and_loss
import calendar
from .models import Expense


@user_passes_test(
    lambda user: user.is_active and user.is_staff
)
def dashboard(request):
    """Display key inventory and sales figures for shop staff."""

    # Get the current date in the configured Django timezone.
    today = timezone.localdate()

    # Count all products registered in the system.
    total_products = Product.objects.count()

    # Find products that have reached or fallen below minimum stock.
    low_stock_products = Product.objects.filter(
        quantity__lte=models.F("minimum_stock")
    ).order_by("quantity", "name")

    # Only completed sales count as actual sales.
    completed_sales_today = Sale.objects.filter(
        status="COMPLETED",
        sale_date__date=today,
    )

    # Calculate the total value of completed sales today.
    sales_total = Decimal("0.00")

    for sale in completed_sales_today:
        sales_total += Decimal(str(sale.total_amount))

    # Calculate payments actually received today.
    payments_today = Payment.objects.filter(
        received_at__date=today,
    ).aggregate(total=Sum("amount"))["total"] or Decimal("0.00")

    # Calculate outstanding balances on completed invoices.
    outstanding_balance = Decimal("0.00")

    for sale in Sale.objects.filter(status="COMPLETED").prefetch_related(
        "items",
        "payments",
    ):
        invoice_total = Decimal(str(sale.total_amount))
        amount_paid = sale.payments.aggregate(
            total=Sum("amount")
        )["total"] or Decimal("0.00")

        balance = invoice_total - amount_paid

        if balance > Decimal("0.00"):
            outstanding_balance += balance

    # Display the five most recent completed sales.
    recent_sales = completed_sales_today.order_by("-sale_date")[:5]

    context = {
        "today": today,
        "total_products": total_products,
        "low_stock_products": low_stock_products,
        "low_stock_count": low_stock_products.count(),
        "sales_count_today": completed_sales_today.count(),
        "sales_total_today": sales_total,
        "payments_today": payments_today,
        "outstanding_balance": outstanding_balance,
        "recent_sales": recent_sales,
    }

    return render(request, "products/dashboard.html", context)



@user_passes_test(
    lambda user: user.is_active and user.is_superuser
)
def profit_loss_report(request):
    """Display a profit and loss report for a selected period."""

    today = timezone.localdate()
    period = request.GET.get("period", "month")
    error = None

    # Default to the current month.
    start_date = today.replace(day=1)
    end_date = today

    # Allow the user to select a standard reporting period.
    if period == "day":
        start_date = today
        end_date = today

    elif period == "month":
        start_date = today.replace(day=1)
        end_date = today

    elif period == "year":
        start_date = today.replace(month=1, day=1)
        end_date = today

    elif period == "custom":
        try:
            start_date = date.fromisoformat(
                request.GET.get("start_date", "")
            )
            end_date = date.fromisoformat(
                request.GET.get("end_date", "")
            )

            if start_date > end_date:
                error = "The start date cannot be after the end date."

        except (ValueError, TypeError):
            error = "Please select valid start and end dates."

    else:
        period = "month"

    report = None

    # Default chart data prevents UnboundLocalError
    # when the report dates are invalid.
    financial_chart = {
        "labels": [
            "Net Sales",
            "Net COGS",
            "Operating Expenses",
        ],
        "values": [0, 0, 0],
    }

    expense_chart = {
        "labels": [],
        "values": [],
    }

    profit_chart = {
        "labels": [],
        "values": [],
    }

    # Calculate the report only when the dates are valid.
    if error is None:
        report = profit_and_loss(start_date, end_date)

###

    # --------------------------------------------------------
    # CHART 1: FINANCIAL COMPARISON
    # --------------------------------------------------------
    financial_chart = {
        "labels": [
            "Net Sales",
            "Net COGS",
            "Operating Expenses",
        ],
        "values": [
            float(report["net_sales"]) if report else 0,
            float(report["net_cost_of_goods_sold"]) if report else 0,
            float(report["operating_expenses"]) if report else 0,
        ],
    }

    # --------------------------------------------------------
    # CHART 2: OPERATING EXPENSES BY CATEGORY
    # --------------------------------------------------------
    expense_labels = []
    expense_values = []

    if report:
        expenses_by_category = (
            Expense.objects.filter(
                expense_date__gte=start_date,
                expense_date__lte=end_date,
            )
            .values("category")
            .annotate(total=Sum("amount"))
            .order_by("category")
        )

        category_names = dict(Expense.CATEGORY_CHOICES)

        for expense in expenses_by_category:
            expense_labels.append(
                category_names.get(
                    expense["category"],
                    expense["category"],
                )
            )
            expense_values.append(float(expense["total"]))

    expense_chart = {
        "labels": expense_labels,
        "values": expense_values,
    }

    # --------------------------------------------------------
    # CHART 3: MONTHLY NET PROFIT TREND
    # --------------------------------------------------------
    profit_labels = []
    profit_values = []

    if report:
        month_cursor = start_date.replace(day=1)

        while month_cursor <= end_date:
            month_last_day = calendar.monthrange(
                month_cursor.year,
                month_cursor.month,
            )[1]

            month_end = month_cursor.replace(day=month_last_day)

            # Keep the first and last months inside the selected range.
            period_start = max(month_cursor, start_date)
            period_end = min(month_end, end_date)

            month_report = profit_and_loss(
                period_start,
                period_end,
            )

            profit_labels.append(
                month_cursor.strftime("%b %Y")
            )

            if month_report["costs_complete"]:
                profit_values.append(
                    float(month_report["net_profit"])
                )
            else:
                # null tells Chart.js that profit is unavailable.
                profit_values.append(None)

            # Move to the first day of the next month.
            if month_cursor.month == 12:
                month_cursor = month_cursor.replace(
                    year=month_cursor.year + 1,
                    month=1,
                    day=1,
                )
            else:
                month_cursor = month_cursor.replace(
                    month=month_cursor.month + 1,
                    day=1,
                )

    profit_chart = {
        "labels": profit_labels,
        "values": profit_values,
    }


    # Prepare the data required by the template.
    context = {
        "report": report,
        "period": period,
        "start_date": start_date,
        "end_date": end_date,
        "error": error,
        "financial_chart": financial_chart,
        "expense_chart": expense_chart,
        "profit_chart": profit_chart,
    }

    # Render the page after all chart data has been calculated.
    return render(
        request,
        "products/profit_loss.html",
        context,
    )
