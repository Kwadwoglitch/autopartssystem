# ============================================================
# PROFIT AND LOSS REPORTING
# Calculates sales, returns, cost of goods sold, and expenses.
# ============================================================

from decimal import Decimal

from .models import (
    Sale,
    SaleItem,
    SaleReturn,
    SaleReturnItem,
    Expense,
)


ZERO = Decimal("0.00")


def profit_and_loss(start_date, end_date):
    """
    Calculate profit and loss for an inclusive date range.

    Sales use sale_date.
    Returns use the date the return was recorded.
    Expenses use expense_date.

    Historical costs that are unknown are flagged. When any
    relevant cost is unknown, gross profit and net profit are
    returned as None rather than being presented as definitive.
    """

    if start_date > end_date:
        raise ValueError(
            "The start date cannot be after the end date."
        )

    # --------------------------------------------------------
    # 1. SALES REVENUE
    # Include completed sales only.
    # --------------------------------------------------------

    sales = Sale.objects.filter(
        status="COMPLETED",
        sale_date__date__gte=start_date,
        sale_date__date__lte=end_date,
    ).prefetch_related("items")

    sales_revenue = ZERO
    cost_of_goods_sold = ZERO
    unknown_cost_units = 0

    for sale in sales:
        for item in sale.items.all():
            sales_revenue += (
                Decimal(item.quantity) * item.unit_price
            )

            if item.unit_cost is None:
                unknown_cost_units += item.quantity
            else:
                cost_of_goods_sold += (
                    Decimal(item.quantity) * item.unit_cost
                )

    # --------------------------------------------------------
    # 2. SALES RETURNS
    # Deduct accepted returns recorded during this period.
    # --------------------------------------------------------

    returns = SaleReturn.objects.filter(
        created_at__date__gte=start_date,
        created_at__date__lte=end_date,
        sale__status="COMPLETED",
    ).prefetch_related("items")

    sales_returns = ZERO
    returned_inventory_cost = ZERO

    for sale_return in returns:
        for item in sale_return.items.all():
            # Every accepted return reduces sales revenue.
            sales_returns += (
                Decimal(item.quantity) * item.unit_price
            )

            # Reverse COGS only for inventory that is restocked.
            if item.restock:
                if item.unit_cost is None:
                    unknown_cost_units += item.quantity
                else:
                    returned_inventory_cost += (
                        Decimal(item.quantity) * item.unit_cost
                    )

    # Net COGS may be negative if eligible inventory returns
    # during this period relate to sales from earlier periods.
    net_cost_of_goods_sold = (
        cost_of_goods_sold - returned_inventory_cost
    )

    # --------------------------------------------------------
    # 3. NET SALES AND GROSS PROFIT
    # --------------------------------------------------------

    net_sales = sales_revenue - sales_returns

    # --------------------------------------------------------
    # 4. OPERATING EXPENSES
    # Inventory purchases are not operating expenses here.
    # --------------------------------------------------------

    expenses = Expense.objects.filter(
        expense_date__gte=start_date,
        expense_date__lte=end_date,
    )

    expense_total = sum(
        expenses.values_list("amount", flat=True),
        start=ZERO,
    )

    # --------------------------------------------------------
    # 5. PROFIT
    # Do not claim definitive profit if historical costs are
    # missing from relevant sales or restocked returns.
    # --------------------------------------------------------

    costs_complete = unknown_cost_units == 0

    if costs_complete:
        gross_profit = net_sales - net_cost_of_goods_sold
        net_profit = gross_profit - expense_total
    else:
        gross_profit = None
        net_profit = None

    return {
        "start_date": start_date,
        "end_date": end_date,
        "sales_revenue": sales_revenue,
        "sales_returns": sales_returns,
        "net_sales": net_sales,
        "cost_of_goods_sold": cost_of_goods_sold,
        "returned_inventory_cost": returned_inventory_cost,
        "net_cost_of_goods_sold": net_cost_of_goods_sold,
        "gross_profit": gross_profit,
        "operating_expenses": expense_total,
        "net_profit": net_profit,
        "unknown_cost_units": unknown_cost_units,
        "costs_complete": costs_complete,
        "sales_count": sales.count(),
        "returns_count": returns.count(),
        "expense_count": expenses.count(),
    }
