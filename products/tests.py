
# ============================================================
# IMPORTS AND TEST DEPENDENCIES
# ============================================================

from datetime import date
from decimal import Decimal
from django.contrib import admin
from django.test import RequestFactory
from .admin import SaleItemInline
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.db import IntegrityError, transaction
from django.db.models import Sum
from django.test import TestCase
from django.urls import reverse

from .models import (
    Brand,
    Category,
    Product,
    Supplier,
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
    adjust_stock,
    receive_purchase,
    complete_sale,
    record_payment,
    cancel_draft_sale,
    process_sale_return,
    record_refund,
)
from .reporting import profit_and_loss

User = get_user_model()


# ============================================================
# SHARED TEST SETUP
# Common database records and reusable helper methods
# ============================================================

class CarPartsTestBase(TestCase):
    """Create reusable database records and helper methods."""

    def setUp(self):
        self.brand = Brand.objects.create(
            name="Toyota Genuine",
            description="Test brand",
        )

        self.category = Category.objects.create(
            name="Engine Parts",
            description="Test category",
        )

        self.product = Product.objects.create(
            name="Oil Filter",
            part_number="TOY-OF-001",
            brand=self.brand,
            category=self.category,
            cost_price=Decimal("50.00"),
            selling_price=Decimal("80.00"),
            quantity=10,
            minimum_stock=2,
        )

        self.supplier = Supplier.objects.create(
            name="Test Auto Supplier",
            phone="0240000000",
        )

        self.customer = Customer.objects.create(
            name="Test Customer",
            customer_type="INDIVIDUAL",
            phone="0200000000",
        )

    def create_sale(self, quantity=2, unit_price="80.00"):
        """Create a draft sale with one sale item."""

        # Keep the first invoice as INV-TEST-001,
        # the second as INV-TEST-002, and so on.
        invoice_number = (
            f"INV-TEST-{Sale.objects.count() + 1:03d}"
        )

        sale = Sale.objects.create(
            customer=self.customer,
            invoice_number=invoice_number,
        )

        SaleItem.objects.create(
            sale=sale,
            product=self.product,
            quantity=quantity,
            unit_price=Decimal(unit_price),
        )

        return sale


# ============================================================
# STOCK ADJUSTMENT TESTS
# Verify quantity changes, validation, and audit records
# ============================================================

class StockAdjustmentTests(CarPartsTestBase):

    def test_increase_stock(self):
        product = adjust_stock(
            self.product.id,
            15,
            "Physical stock count",
        )

        product.refresh_from_db()

        self.assertEqual(product.quantity, 15)

        record = InventoryTransaction.objects.get(
            reference=f"ADJUSTMENT-PRODUCT-{self.product.id}",
        )

        self.assertEqual(record.transaction_type, "ADJUSTMENT")
        self.assertEqual(record.quantity, 5)
        self.assertIn("Physical stock count", record.notes)

    def test_decrease_stock(self):
        product = adjust_stock(
            self.product.id,
            7,
            "Damaged stock removed",
        )

        product.refresh_from_db()

        self.assertEqual(product.quantity, 7)

        record = InventoryTransaction.objects.get(
            reference=f"ADJUSTMENT-PRODUCT-{self.product.id}",
        )

        self.assertEqual(record.quantity, -3)

    def test_negative_quantity_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Stock quantity cannot be negative.",
        ):
            adjust_stock(
                self.product.id,
                -1,
                "Invalid negative stock",
            )

    def test_missing_reason_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "A reason is required for stock adjustment.",
        ):
            adjust_stock(
                self.product.id,
                15,
                "   ",
            )

    def test_unchanged_quantity_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "The new quantity is the same as the current quantity.",
        ):
            adjust_stock(
                self.product.id,
                10,
                "Unchanged stock",
            )

    def test_decimal_quantity_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "The new stock quantity must be a whole number.",
        ):
            adjust_stock(
                self.product.id,
                8.5,
                "Invalid decimal quantity",
            )

    def test_invalid_adjustment_changes_nothing(self):
        original_quantity = self.product.quantity
        original_transactions = InventoryTransaction.objects.count()

        with self.assertRaises(ValueError):
            adjust_stock(
                self.product.id,
                -5,
                "Invalid adjustment",
            )

        self.product.refresh_from_db()

        self.assertEqual(
            self.product.quantity,
            original_quantity,
        )
        self.assertEqual(
            InventoryTransaction.objects.count(),
            original_transactions,
        )

    def test_nonexistent_product_is_rejected(self):
        original_transactions = InventoryTransaction.objects.count()

        with self.assertRaises(Product.DoesNotExist):
            adjust_stock(
                999999,
                15,
                "Testing nonexistent product",
            )

        self.assertEqual(
            InventoryTransaction.objects.count(),
            original_transactions,
        )



    def test_stock_change_rolls_back_if_history_creation_fails(self):
        from unittest.mock import patch

        original_quantity = self.product.quantity
        original_transactions = InventoryTransaction.objects.count()

        with patch(
            "products.services.InventoryTransaction.objects.create",
            side_effect=RuntimeError("Simulated history failure"),
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "Simulated history failure",
            ):
                adjust_stock(
                    self.product.id,
                    15,
                    "Testing atomic rollback",
                )

        self.product.refresh_from_db()

        self.assertEqual(
            self.product.quantity,
            original_quantity,
        )
        self.assertEqual(
            InventoryTransaction.objects.count(),
            original_transactions,
        )


# ============================================================
# SALE SERVICE TESTS
# Verify sale completion, stock deduction, and cancellation
# ============================================================

class SaleServiceTests(CarPartsTestBase):

    def test_complete_sale_deducts_stock(self):
        sale = self.create_sale(quantity=3)

        complete_sale(sale)
        sale.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, 7)
        self.assertEqual(sale.status, "COMPLETED")

        transaction_record = InventoryTransaction.objects.get(
            reference=f"SALE-{sale.invoice_number}",
        )

        self.assertEqual(transaction_record.quantity, -3)
        self.assertEqual(transaction_record.transaction_type, "SALE")

    def test_sale_cannot_reduce_stock_below_zero(self):
        sale = self.create_sale(quantity=11)

        with self.assertRaisesMessage(
            ValueError,
            "Insufficient stock for Oil Filter.",
        ):
            complete_sale(sale)

        self.product.refresh_from_db()
        sale.refresh_from_db()

        self.assertEqual(self.product.quantity, 10)
        self.assertEqual(sale.status, "DRAFT")
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"SALE-{sale.invoice_number}",
            ).exists()
        )

    def test_sale_rolls_back_all_stock_changes_if_later_item_fails(self):
        sale = self.create_sale(quantity=2)

        second_product = Product.objects.create(
            brand=self.brand,
            category=self.category,
            name="Brake Pad",
            part_number="TOY-BP-SALE-001",
            cost_price=Decimal("60.00"),
            selling_price=Decimal("100.00"),
            quantity=1,
            minimum_stock=1,
        )

        SaleItem.objects.create(
            sale=sale,
            product=second_product,
            quantity=2,
            unit_price=Decimal("100.00"),
        )

        with self.assertRaisesMessage(
            ValueError,
            "Insufficient stock for Brake Pad.",
        ):
            complete_sale(sale)

        self.product.refresh_from_db()
        second_product.refresh_from_db()
        sale.refresh_from_db()

        self.assertEqual(self.product.quantity, 10)
        self.assertEqual(second_product.quantity, 1)
        self.assertEqual(sale.status, "DRAFT")
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"SALE-{sale.invoice_number}",
            ).exists()
        )

    def test_sale_without_items_cannot_be_completed(self):
        sale = Sale.objects.create(
            customer=self.customer,
            invoice_number="INV-EMPTY-001",
        )

        with self.assertRaisesMessage(
            ValueError,
            "Cannot complete a sale without items.",
        ):
            complete_sale(sale)

        sale.refresh_from_db()
        self.assertEqual(sale.status, "DRAFT")

    def test_completed_sale_cannot_be_completed_again(self):
        sale = self.create_sale(quantity=2)

        complete_sale(sale)

        with self.assertRaisesMessage(
            ValueError,
            "This sale has already been completed.",
        ):
            complete_sale(sale)

        self.product.refresh_from_db()
        self.assertEqual(self.product.quantity, 8)

    def test_cancel_draft_sale_changes_status_without_changing_stock(self):
        sale = self.create_sale(quantity=2)

        cancel_draft_sale(sale)

        sale.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(sale.status, "CANCELLED")
        self.assertEqual(self.product.quantity, 10)
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"SALE-{sale.invoice_number}",
            ).exists()
        )

    def test_completed_sale_cannot_be_cancelled_as_draft(self):
        sale = self.create_sale(quantity=2)
        complete_sale(sale)

        with self.assertRaisesMessage(
            ValueError,
            "A completed sale cannot be cancelled through this workflow.",
        ):
            cancel_draft_sale(sale)

        sale.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(sale.status, "COMPLETED")
        self.assertEqual(self.product.quantity, 8)

    def test_cancelled_sale_cannot_be_cancelled_twice(self):
        sale = self.create_sale(quantity=2)

        cancel_draft_sale(sale)

        with self.assertRaisesMessage(
            ValueError,
            "This sale has already been cancelled.",
        ):
            cancel_draft_sale(sale)

        sale.refresh_from_db()
        self.assertEqual(sale.status, "CANCELLED")


    def test_complete_sale_records_unit_cost_snapshot(self):
        sale = self.create_sale(quantity=2)

        complete_sale(sale)

        sale_item = sale.items.get()

        self.assertEqual(
            sale_item.unit_cost,
            Decimal("50.00"),
        )

        # Later cost changes must not change the completed sale.
        self.product.cost_price = Decimal("65.00")
        self.product.save(update_fields=["cost_price"])

        sale_item.refresh_from_db()

        self.assertEqual(
            sale_item.unit_cost,
            Decimal("50.00"),
        )



# ============================================================
# PAYMENT SERVICE TESTS
# Verify payment recording, balance calculations, and validation
# ============================================================

class PaymentServiceTests(CarPartsTestBase):

    def setUp(self):
        super().setUp()
        self.sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(self.sale)
        self.sale.refresh_from_db()

    def test_sale_total_is_calculated_correctly(self):
        self.assertEqual(
            self.sale.total_amount,
            Decimal("160.00"),
        )

    def test_full_payment_marks_sale_as_paid(self):
        payment = record_payment(
            self.sale,
            amount="160.00",
            payment_method="CASH",
        )

        self.sale.refresh_from_db()

        self.assertEqual(payment.amount, Decimal("160.00"))
        self.assertEqual(self.sale.payment_status, "PAID")

    def test_partial_payment_marks_sale_as_partial(self):
        payment = record_payment(
            self.sale,
            amount="60.00",
            payment_method="MOBILE_MONEY",
            reference="MOMO-001",
        )

        self.sale.refresh_from_db()

        self.assertEqual(payment.amount, Decimal("60.00"))
        self.assertEqual(self.sale.payment_status, "PARTIAL")
        self.assertEqual(
            self.sale.payments.aggregate(total=Sum("amount"))["total"],
            Decimal("60.00"),
        )

    def test_overpayment_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Payment exceeds the outstanding balance of 160.00.",
        ):
            record_payment(
                self.sale,
                amount="200.00",
                payment_method="CASH",
            )

        self.assertFalse(Payment.objects.filter(sale=self.sale).exists())

    def test_zero_payment_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Payment amount must be greater than zero.",
        ):
            record_payment(
                self.sale,
                amount="0.00",
                payment_method="CASH",
            )

    def test_negative_payment_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Payment amount must be greater than zero.",
        ):
            record_payment(
                self.sale,
                amount="-10.00",
                payment_method="CASH",
            )

    def test_invalid_payment_amount_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Enter a valid payment amount.",
        ):
            record_payment(
                self.sale,
                amount="not-a-number",
                payment_method="CASH",
            )

    def test_non_finite_payment_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            record_payment(
                self.sale,
                amount="NaN",
                payment_method="CASH",
            )

        self.assertFalse(Payment.objects.filter(sale=self.sale).exists())

    def test_invalid_payment_method_is_rejected(self):
        with self.assertRaises(ValueError):
            record_payment(
                self.sale,
                amount="50.00",
                payment_method="INVALID",
            )

        self.assertFalse(Payment.objects.filter(sale=self.sale).exists())

    def test_duplicate_mobile_money_reference_is_rejected(self):
        record_payment(
            self.sale,
            amount="30.00",
            payment_method="MOBILE_MONEY",
            reference="MOMO-DUPLICATE",
        )

        with self.assertRaises(ValueError):
            record_payment(
                self.sale,
                amount="20.00",
                payment_method="MOBILE_MONEY",
                reference="MOMO-DUPLICATE",
            )

        self.assertEqual(
            Payment.objects.filter(sale=self.sale).count(),
            1,
        )

    def test_payment_cannot_be_recorded_for_draft_sale(self):
        draft_sale = self.create_sale(
            quantity=1,
            unit_price="80.00",
        )

        with self.assertRaises(ValueError):
            record_payment(
                draft_sale,
                amount="40.00",
                payment_method="CASH",
            )

        self.assertFalse(
            Payment.objects.filter(sale=draft_sale).exists()
        )



# ============================================================
# SALE RETURN SERVICE TESTS
# Verify return validation, restocking, and audit records
# ============================================================

class SaleReturnServiceTests(CarPartsTestBase):

    def setUp(self):
        super().setUp()

        self.sale = self.create_sale(
            quantity=3,
            unit_price="80.00",
        )
        complete_sale(self.sale)
        self.sale.refresh_from_db()

        self.sale_item = self.sale.items.get()

    def test_return_restocked_item_increases_stock(self):
        returned_item = {
            "sale_item_id": self.sale_item.id,
            "quantity": 1,
            "restock": True,
        }

        sale_return = process_sale_return(
            sale=self.sale,
            items=[returned_item],
            reason="Customer returned an unused part",
        )

        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, 8)
        self.assertEqual(sale_return.total_amount, Decimal("80.00"))

        return_item = sale_return.items.get()

        self.assertEqual(return_item.quantity, 1)
        self.assertEqual(return_item.unit_price, Decimal("80.00"))
        self.assertTrue(return_item.restock)

        self.assertEqual(
            return_item.unit_cost,
            Decimal("50.00"),
        )

        transaction_record = InventoryTransaction.objects.get(
            reference=f"RETURN-{sale_return.pk}",
        )

        self.assertEqual(transaction_record.transaction_type, "RETURN")
        self.assertEqual(transaction_record.quantity, 1)



    def test_failed_return_rolls_back_return_and_stock_changes(self):
        from unittest.mock import patch

        self.product.refresh_from_db()
        original_quantity = self.product.quantity

        with patch(
            "products.services.InventoryTransaction.objects.create",
            side_effect=RuntimeError("Simulated audit-record failure"),
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "Simulated audit-record failure",
            ):
                process_sale_return(
                    sale=self.sale,
                    items=[
                        {
                            "sale_item_id": self.sale_item.id,
                            "quantity": 1,
                            "restock": True,
                        }
                    ],
                    reason="Testing rollback after audit failure",
                )

        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, original_quantity)
        self.assertFalse(
            SaleReturn.objects.filter(sale=self.sale).exists()
        )


    def test_return_preserves_original_cost_after_product_cost_changes(self):
        # The completed sale recorded a unit cost of 50.00.
        # Change the product's current cost to simulate a later purchase.
        self.product.cost_price = Decimal("65.00")
        self.product.save(update_fields=["cost_price"])

        sale_return = process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale_item.id,
                    "quantity": 1,
                    "restock": True,
                }
            ],
            reason="Testing historical cost preservation",
        )

        return_item = sale_return.items.get()

        # The return must use the original sale cost, not 65.00.
        self.assertEqual(
            return_item.unit_cost,
            Decimal("50.00"),
        )

    def test_return_preserves_unknown_historical_cost_as_none(self):
        # Simulate an old sale whose historical cost is unknown.
        self.sale_item.unit_cost = None
        self.sale_item.save(update_fields=["unit_cost"])

        sale_return = process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale_item.id,
                    "quantity": 1,
                    "restock": False,
                }
            ],
            reason="Testing unknown historical cost",
        )

        return_item = sale_return.items.get()

        # Do not invent a cost for a historical sale.
        self.assertIsNone(return_item.unit_cost)


    def test_return_without_restock_does_not_change_stock(self):
        # Reload the product because completing the sale reduced its stock.
        self.product.refresh_from_db()
        original_quantity = self.product.quantity

        sale_return = process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale_item.id,
                    "quantity": 1,
                    "restock": False,
                }
            ],
            reason="Part returned but not suitable for resale",
        )

        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, original_quantity)
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"RETURN-{sale_return.pk}",
            ).exists()
        )


    def test_return_quantity_cannot_exceed_quantity_sold(self):
        with self.assertRaisesMessage(
            ValueError,
            "Only 3 unit(s) remain eligible.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[
                    {
                        "sale_item_id": self.sale_item.id,
                        "quantity": 4,
                        "restock": True,
                    }
                ],
                reason="Returning too many parts",
            )

        self.assertFalse(SaleReturn.objects.filter(sale=self.sale).exists())

    def test_previous_returns_reduce_remaining_returnable_quantity(self):
        process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale_item.id,
                    "quantity": 2,
                    "restock": False,
                }
            ],
            reason="First partial return",
        )

        process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale_item.id,
                    "quantity": 1,
                    "restock": False,
                }
            ],
            reason="Final eligible return",
        )

        with self.assertRaisesMessage(
            ValueError,
            "Only 0 unit(s) remain eligible.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[
                    {
                        "sale_item_id": self.sale_item.id,
                        "quantity": 1,
                        "restock": False,
                    }
                ],
                reason="Attempt to return an already returned part",
            )

    def test_draft_sale_cannot_be_returned(self):
        draft_sale = self.create_sale(quantity=1)

        with self.assertRaisesMessage(
            ValueError,
            "Only completed sales can have returns.",
        ):
            process_sale_return(
                sale=draft_sale,
                items=[
                    {
                        "sale_item_id": draft_sale.items.get().id,
                        "quantity": 1,
                        "restock": False,
                    }
                ],
                reason="Invalid draft sale return",
            )

    def test_blank_return_reason_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "A return reason is required.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[
                    {
                        "sale_item_id": self.sale_item.id,
                        "quantity": 1,
                        "restock": False,
                    }
                ],
                reason="   ",
            )

    def test_return_without_items_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "A return must contain at least one item.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[],
                reason="No items supplied",
            )

    def test_return_item_must_belong_to_the_sale(self):
        another_sale = self.create_sale(quantity=1)
        another_item = another_sale.items.get()

        with self.assertRaisesMessage(
            ValueError,
            "A return item does not belong to this sale.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[
                    {
                        "sale_item_id": another_item.id,
                        "quantity": 1,
                        "restock": False,
                    }
                ],
                reason="Item belongs to another sale",
            )

    def test_duplicate_sale_item_entries_are_rejected(self):
        item_data = {
            "sale_item_id": self.sale_item.id,
            "quantity": 1,
            "restock": False,
        }

        with self.assertRaisesMessage(
            ValueError,
            "Do not include the same sale item more than once.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[item_data, item_data],
                reason="Duplicate item",
            )

    def test_zero_return_quantity_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Return quantities must be positive whole numbers.",
        ):
            process_sale_return(
                sale=self.sale,
                items=[
                    {
                        "sale_item_id": self.sale_item.id,
                        "quantity": 0,
                        "restock": False,
                    }
                ],
                reason="Invalid zero quantity",
            )



# ============================================================
# REFUND SERVICE TESTS
# Verify refund amounts, payment limits, and validation
# ============================================================

class RefundServiceTests(CarPartsTestBase):

    def setUp(self):
        super().setUp()

        self.sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(self.sale)

        self.return_record = process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": self.sale.items.get().id,
                    "quantity": 1,
                    "restock": False,
                }
            ],
            reason="Customer returned one item",
        )

        record_payment(
            self.sale,
            amount="160.00",
            payment_method="CASH",
        )

        self.sale.refresh_from_db()

    def test_valid_refund_is_recorded(self):
        refund = record_refund(
            sale_return=self.return_record,
            amount="80.00",
            refund_method="CASH",
            reference="REFUND-001",
            notes="Refund for returned item",
        )

        self.assertEqual(refund.amount, Decimal("80.00"))
        self.assertEqual(refund.refund_method, "CASH")
        self.assertEqual(refund.reference, "REFUND-001")
        self.assertEqual(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).count(),
            1,
        )

    def test_zero_refund_is_rejected(self):
        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="0.00",
                refund_method="CASH",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )

    def test_negative_refund_is_rejected(self):
        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="-10.00",
                refund_method="CASH",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )

    def test_invalid_refund_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="not-a-number",
                refund_method="CASH",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )

    def test_refund_with_more_than_two_decimal_places_is_rejected(self):
        with self.assertRaisesMessage(
            ValueError,
            "Refund amount cannot have more than two decimal places.",
        ):
            record_refund(
                sale_return=self.return_record,
                amount="10.123",
                refund_method="CASH",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )


    def test_refund_cannot_exceed_return_value(self):
        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="100.00",
                refund_method="CASH",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )

    def test_total_refunds_cannot_exceed_amount_paid(self):
        record_refund(
            sale_return=self.return_record,
            amount="80.00",
            refund_method="CASH",
        )

        # Create a second returned item worth 80.00.
        sale_item = self.sale.items.get()

        second_return = process_sale_return(
            sale=self.sale,
            items=[
                {
                    "sale_item_id": sale_item.id,
                    "quantity": 1,
                    "restock": False,
                }
            ],
            reason="Return of the second item",
        )

        # The original payment was 160.00. The first refund used 80.00.
        # The remaining paid amount is therefore 80.00.
        record_refund(
            sale_return=second_return,
            amount="80.00",
            refund_method="MOBILE_MONEY",
        )

        self.assertEqual(
            Refund.objects.aggregate(total=Sum("amount"))["total"],
            Decimal("160.00"),
        )

    def test_invalid_refund_method_is_rejected(self):
        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="20.00",
                refund_method="INVALID",
            )

        self.assertFalse(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).exists()
        )

    def test_duplicate_refund_reference_is_rejected(self):
        record_refund(
            sale_return=self.return_record,
            amount="20.00",
            refund_method="MOBILE_MONEY",
            reference="REFUND-MOMO-001",
        )

        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="30.00",
                refund_method="MOBILE_MONEY",
                reference="REFUND-MOMO-001",
            )

        self.assertEqual(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).count(),
            1,
        )

    def test_refund_does_not_delete_original_payment(self):
        original_payment_count = Payment.objects.filter(
            sale=self.sale,
        ).count()

        record_refund(
            sale_return=self.return_record,
            amount="40.00",
            refund_method="CASH",
        )

        self.assertEqual(
            Payment.objects.filter(sale=self.sale).count(),
            original_payment_count,
        )

    def test_refund_cannot_exceed_remaining_return_value(self):
        record_refund(
            sale_return=self.return_record,
            amount="50.00",
            refund_method="CASH",
        )

        with self.assertRaises(ValueError):
            record_refund(
                sale_return=self.return_record,
                amount="40.00",
                refund_method="MOBILE_MONEY",
            )

        self.assertEqual(
            Refund.objects.filter(
                sale_return=self.return_record,
            ).aggregate(total=Sum("amount"))["total"],
            Decimal("50.00"),
        )


    def test_two_partial_refunds_can_use_full_return_value(self):
        record_refund(
            sale_return=self.return_record,
            amount="30.00",
            refund_method="CASH",
        )

        record_refund(
            sale_return=self.return_record,
            amount="50.00",
            refund_method="MOBILE_MONEY",
        )

        total_refunded = Refund.objects.filter(
            sale_return=self.return_record,
        ).aggregate(total=Sum("amount"))["total"]

        self.assertEqual(
            total_refunded,
            Decimal("80.00"),
        )


# ============================================================
# PURCHASE SERVICE TESTS
# Verify receiving, stock updates, audit records, and rollback
# ============================================================

class PurchaseServiceTests(CarPartsTestBase):

    def create_purchase(self, status="DRAFT"):
        return Purchase.objects.create(
            supplier=self.supplier,
            invoice_number="SUP-INV-001",
            purchase_date=date.today(),
            status=status,
        )

    def test_receive_purchase_increases_stock_and_updates_status(self):
        purchase = self.create_purchase()

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=5,
            unit_cost=Decimal("45.00"),
        )

        received_purchase = receive_purchase(purchase)

        self.product.refresh_from_db()
        received_purchase.refresh_from_db()

        self.assertEqual(self.product.quantity, 15)
        self.assertEqual(received_purchase.status, "RECEIVED")

        transaction_record = InventoryTransaction.objects.get(
            reference=f"PURCHASE-{purchase.id}",
        )

        self.assertEqual(transaction_record.transaction_type, "PURCHASE")
        self.assertEqual(transaction_record.quantity, 5)
        self.assertEqual(transaction_record.product, self.product)

    def test_receive_purchase_with_multiple_items_updates_each_product(self):
        purchase = self.create_purchase()

        second_product = Product.objects.create(
            brand=self.brand,
            category=self.category,
            name="Brake Pad",
            part_number="TOY-BP-PURCHASE-001",
            cost_price=Decimal("60.00"),
            selling_price=Decimal("100.00"),
            quantity=4,
            minimum_stock=1,
        )

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=3,
            unit_cost=Decimal("45.00"),
        )

        PurchaseItem.objects.create(
            purchase=purchase,
            product=second_product,
            quantity=6,
            unit_cost=Decimal("55.00"),
        )

        receive_purchase(purchase)

        self.product.refresh_from_db()
        second_product.refresh_from_db()

        self.assertEqual(self.product.quantity, 13)
        self.assertEqual(second_product.quantity, 10)

        transactions = InventoryTransaction.objects.filter(
            reference=f"PURCHASE-{purchase.id}",
        )

        self.assertEqual(transactions.count(), 2)

    def test_empty_purchase_cannot_be_received(self):
        purchase = self.create_purchase()

        with self.assertRaisesMessage(
            ValueError,
            "An empty purchase cannot be received.",
        ):
            receive_purchase(purchase)

        purchase.refresh_from_db()
        self.product.refresh_from_db()

        self.assertEqual(purchase.status, "DRAFT")
        self.assertEqual(self.product.quantity, 10)
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"PURCHASE-{purchase.id}",
            ).exists()
        )

    def test_purchase_cannot_be_received_twice(self):
        purchase = self.create_purchase()

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=5,
            unit_cost=Decimal("45.00"),
        )

        receive_purchase(purchase)

        with self.assertRaisesMessage(
            ValueError,
            "This purchase has already been received.",
        ):
            receive_purchase(purchase)

        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, 15)
        self.assertEqual(
            InventoryTransaction.objects.filter(
                reference=f"PURCHASE-{purchase.id}",
            ).count(),
            1,
        )

    def test_cancelled_purchase_cannot_be_received(self):
        purchase = self.create_purchase(status="CANCELLED")

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=5,
            unit_cost=Decimal("45.00"),
        )

        with self.assertRaisesMessage(
            ValueError,
            "A cancelled purchase cannot be received.",
        ):
            receive_purchase(purchase)

        self.product.refresh_from_db()
        purchase.refresh_from_db()

        self.assertEqual(self.product.quantity, 10)
        self.assertEqual(purchase.status, "CANCELLED")
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"PURCHASE-{purchase.id}",
            ).exists()
        )

    def test_failed_receiving_rolls_back_stock_and_transactions(self):
        from unittest.mock import patch

        purchase = self.create_purchase()

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=5,
            unit_cost=Decimal("45.00"),
        )

        original_quantity = self.product.quantity

        with patch(
            "products.services.InventoryTransaction.objects.create",
            side_effect=RuntimeError("Simulated audit-record failure"),
        ):
            with self.assertRaisesMessage(
                RuntimeError,
                "Simulated audit-record failure",
            ):
                receive_purchase(purchase)

        self.product.refresh_from_db()
        purchase.refresh_from_db()

        self.assertEqual(self.product.quantity, original_quantity)
        self.assertEqual(purchase.status, "DRAFT")
        self.assertFalse(
            InventoryTransaction.objects.filter(
                reference=f"PURCHASE-{purchase.id}",
            ).exists()
        )

    def test_receive_purchase_recalculates_moving_average_cost(self):
        # Existing stock: 10 units at GH₵50 each.
        purchase = Purchase.objects.create(
            supplier=self.supplier,
            purchase_date=date.today(),
        )

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=10,
            unit_cost=Decimal("70.00"),
        )

        receive_purchase(purchase)

        self.product.refresh_from_db()

        # (10 * 50 + 10 * 70) / 20 = 60
        self.assertEqual(self.product.quantity, 20)
        self.assertEqual(
            self.product.cost_price,
            Decimal("60.00"),
        )

    def test_receive_purchase_with_zero_stock_uses_purchase_cost(self):
        self.product.quantity = 0
        self.product.cost_price = Decimal("50.00")
        self.product.save(
            update_fields=["quantity", "cost_price"]
        )

        purchase = Purchase.objects.create(
            supplier=self.supplier,
            purchase_date=date.today(),
        )

        PurchaseItem.objects.create(
            purchase=purchase,
            product=self.product,
            quantity=5,
            unit_cost=Decimal("35.00"),
        )

        receive_purchase(purchase)

        self.product.refresh_from_db()

        self.assertEqual(self.product.quantity, 5)
        self.assertEqual(
            self.product.cost_price,
            Decimal("35.00"),
        )


# ============================================================
# DATABASE CONSTRAINT TESTS
# Verify that the database rejects invalid records directly.
# These tests bypass the service functions intentionally.
# ============================================================

class DatabaseConstraintTests(CarPartsTestBase):
    """Test database constraints independently of service logic."""

    def create_return(self):
        """Create a sale and a return for database constraint tests."""
        sale = self.create_sale(quantity=2)
        sale_item = sale.items.get()

        sale_return = SaleReturn.objects.create(
            sale=sale,
            reason="Database constraint test",
        )

        return sale, sale_item, sale_return

    def assert_database_rejects(self, create_record):
        """Assert that a database constraint rejects a record."""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                create_record()

    # --------------------------------------------------------
    # PRODUCT UNIQUENESS
    # --------------------------------------------------------

    def test_duplicate_product_name_is_rejected(self):
        self.assert_database_rejects(
            lambda: Product.objects.create(
                name="Oil Filter",
                part_number="TOY-OF-DUP-NAME",
                brand=self.brand,
                category=self.category,
                cost_price=Decimal("10.00"),
                selling_price=Decimal("15.00"),
            )
        )

    def test_duplicate_product_part_number_is_rejected(self):
        self.assert_database_rejects(
            lambda: Product.objects.create(
                name="Another Oil Filter",
                part_number="TOY-OF-001",
                brand=self.brand,
                category=self.category,
                cost_price=Decimal("10.00"),
                selling_price=Decimal("15.00"),
            )
        )

    def test_duplicate_nonempty_barcode_is_rejected(self):
        Product.objects.filter(pk=self.product.pk).update(
            barcode="BARCODE-001"
        )

        self.assert_database_rejects(
            lambda: Product.objects.create(
                name="Second Oil Filter",
                part_number="TOY-OF-002",
                barcode="BARCODE-001",
                brand=self.brand,
                category=self.category,
                cost_price=Decimal("10.00"),
                selling_price=Decimal("15.00"),
            )
        )

    # --------------------------------------------------------
    # INVENTORY TRANSACTION CONSTRAINTS
    # --------------------------------------------------------

    def test_zero_inventory_transaction_quantity_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="ADJUSTMENT",
                quantity=0,
            )
        )

    def test_positive_and_negative_adjustments_are_allowed(self):
        positive = InventoryTransaction.objects.create(
            product=self.product,
            transaction_type="ADJUSTMENT",
            quantity=5,
        )

        negative = InventoryTransaction.objects.create(
            product=self.product,
            transaction_type="ADJUSTMENT",
            quantity=-3,
        )

        self.assertEqual(positive.quantity, 5)
        self.assertEqual(negative.quantity, -3)

    def test_negative_purchase_transaction_quantity_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="PURCHASE",
                quantity=-5,
            )
        )

    def test_positive_sale_transaction_quantity_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="SALE",
                quantity=5,
            )
        )

    def test_positive_damage_transaction_quantity_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="DAMAGE",
                quantity=5,
            )
        )

    def test_negative_return_transaction_quantity_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="RETURN",
                quantity=-5,
            )
        )

    def test_invalid_inventory_transaction_type_is_rejected(self):
        self.assert_database_rejects(
            lambda: InventoryTransaction.objects.create(
                product=self.product,
                transaction_type="INVALID",
                quantity=1,
            )
        )


    # --------------------------------------------------------
    # PAYMENT AMOUNT CONSTRAINTS
    # --------------------------------------------------------

    def test_zero_payment_amount_is_rejected(self):
        sale = self.create_sale()

        self.assert_database_rejects(
            lambda: Payment.objects.create(
                sale=sale,
                amount=Decimal("0.00"),
                payment_method="CASH",
            )
        )

    def test_negative_payment_amount_is_rejected(self):
        sale = self.create_sale()

        self.assert_database_rejects(
            lambda: Payment.objects.create(
                sale=sale,
                amount=Decimal("-1.00"),
                payment_method="CASH",
            )
        )



    # --------------------------------------------------------
    # PAYMENT REFERENCE CONSTRAINTS
    # --------------------------------------------------------

    def test_duplicate_payment_method_reference_is_rejected(self):
        sale = self.create_sale()

        Payment.objects.create(
            sale=sale,
            amount=Decimal("10.00"),
            payment_method="MOBILE_MONEY",
            reference="DB-TEST-REF-001",
        )

        self.assert_database_rejects(
            lambda: Payment.objects.create(
                sale=sale,
                amount=Decimal("5.00"),
                payment_method="MOBILE_MONEY",
                reference="DB-TEST-REF-001",
            )
        )

    def test_same_payment_reference_can_use_another_method(self):
        sale = self.create_sale()

        Payment.objects.create(
            sale=sale,
            amount=Decimal("10.00"),
            payment_method="MOBILE_MONEY",
            reference="DB-TEST-REF-002",
        )

        # The constraint combines payment_method and reference.
        payment = Payment.objects.create(
            sale=sale,
            amount=Decimal("10.00"),
            payment_method="BANK_TRANSFER",
            reference="DB-TEST-REF-002",
        )

        self.assertEqual(payment.reference, "DB-TEST-REF-002")

    # --------------------------------------------------------
    # SALE RETURN ITEM CONSTRAINTS
    # --------------------------------------------------------

    def test_zero_sale_return_item_quantity_is_rejected(self):
        sale, sale_item, sale_return = self.create_return()

        self.assert_database_rejects(
            lambda: SaleReturnItem.objects.create(
                sale_return=sale_return,
                sale_item=sale_item,
                quantity=0,
                unit_price=Decimal("80.00"),
            )
        )

    def test_negative_sale_return_item_price_is_rejected(self):
        sale, sale_item, sale_return = self.create_return()

        self.assert_database_rejects(
            lambda: SaleReturnItem.objects.create(
                sale_return=sale_return,
                sale_item=sale_item,
                quantity=1,
                unit_price=Decimal("-1.00"),
            )
        )

    # --------------------------------------------------------
    # REFUND CONSTRAINTS
    # --------------------------------------------------------

    def test_zero_refund_amount_is_rejected(self):
        sale, sale_item, sale_return = self.create_return()

        self.assert_database_rejects(
            lambda: Refund.objects.create(
                sale_return=sale_return,
                amount=Decimal("0.00"),
                refund_method="CASH",
            )
        )

    def test_negative_refund_amount_is_rejected(self):
        sale, sale_item, sale_return = self.create_return()

        self.assert_database_rejects(
            lambda: Refund.objects.create(
                sale_return=sale_return,
                amount=Decimal("-1.00"),
                refund_method="CASH",
            )
        )

    def test_duplicate_refund_method_reference_is_rejected(self):
        sale, sale_item, sale_return = self.create_return()

        Refund.objects.create(
            sale_return=sale_return,
            amount=Decimal("10.00"),
            refund_method="MOBILE_MONEY",
            reference="DB-REFUND-001",
        )

        self.assert_database_rejects(
            lambda: Refund.objects.create(
                sale_return=sale_return,
                amount=Decimal("5.00"),
                refund_method="MOBILE_MONEY",
                reference="DB-REFUND-001",
            )
        )



# ============================================================
# PROFIT AND LOSS REPORT TESTS
# Verify accounting calculations and historical cost handling.
# ============================================================

class ProfitAndLossReportTests(CarPartsTestBase):

    def setUp(self):
        super().setUp()
        self.report_date = date.today()

    def test_completed_sale_calculates_revenue_and_cogs(self):
        sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(sale)

        report = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        self.assertEqual(
            report["sales_revenue"],
            Decimal("160.00"),
        )
        self.assertEqual(
            report["sales_returns"],
            Decimal("0.00"),
        )
        self.assertEqual(
            report["net_sales"],
            Decimal("160.00"),
        )
        self.assertEqual(
            report["cost_of_goods_sold"],
            Decimal("100.00"),
        )
        self.assertEqual(
            report["gross_profit"],
            Decimal("60.00"),
        )
        self.assertEqual(
            report["net_profit"],
            Decimal("60.00"),
        )
        self.assertTrue(report["costs_complete"])

    def test_operating_expense_reduces_net_profit(self):
        sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(sale)

        Expense.objects.create(
            expense_date=self.report_date,
            category="UTILITIES",
            description="Test electricity bill",
            amount=Decimal("25.00"),
            payment_method="CASH",
        )

        report = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        self.assertEqual(
            report["gross_profit"],
            Decimal("60.00"),
        )
        self.assertEqual(
            report["operating_expenses"],
            Decimal("25.00"),
        )
        self.assertEqual(
            report["net_profit"],
            Decimal("35.00"),
        )

    def test_restocked_return_reverses_original_cogs(self):
        sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(sale)

        sale_item = sale.items.get()

        process_sale_return(
            sale,
            items=[
                {
                    "sale_item_id": sale_item.pk,
                    "quantity": 1,
                    "restock": True,
                }
            ],
            reason="Test return",
        )

        report = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        self.assertEqual(
            report["sales_revenue"],
            Decimal("160.00"),
        )
        self.assertEqual(
            report["sales_returns"],
            Decimal("80.00"),
        )
        self.assertEqual(
            report["net_sales"],
            Decimal("80.00"),
        )
        self.assertEqual(
            report["cost_of_goods_sold"],
            Decimal("100.00"),
        )
        self.assertEqual(
            report["returned_inventory_cost"],
            Decimal("50.00"),
        )
        self.assertEqual(
            report["net_cost_of_goods_sold"],
            Decimal("50.00"),
        )
        self.assertEqual(
            report["gross_profit"],
            Decimal("30.00"),
        )



    def test_recording_refund_does_not_double_count_sales_return(self):
        sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(sale)

        sale_item = sale.items.get()

        sale_return = process_sale_return(
            sale=sale,
            items=[
                {
                    "sale_item_id": sale_item.pk,
                    "quantity": 1,
                    "restock": True,
                }
            ],
            reason="Regression test return",
        )

        record_payment(
            sale,
            amount="160.00",
            payment_method="CASH",
        )

        report_before_refund = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        record_refund(
            sale_return=sale_return,
            amount="80.00",
            refund_method="CASH",
            reference="PL-REGRESSION-001",
        )

        report_after_refund = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        self.assertEqual(
            report_before_refund["net_sales"],
            Decimal("80.00"),
        )
        self.assertEqual(
            report_after_refund["net_sales"],
            Decimal("80.00"),
        )
        self.assertEqual(
            report_before_refund["net_cost_of_goods_sold"],
            Decimal("50.00"),
        )
        self.assertEqual(
            report_after_refund["net_cost_of_goods_sold"],
            Decimal("50.00"),
        )
        self.assertEqual(
            report_before_refund["gross_profit"],
            Decimal("30.00"),
        )
        self.assertEqual(
            report_after_refund["gross_profit"],
            Decimal("30.00"),
        )


    def test_unknown_historical_cost_marks_profit_incomplete(self):
        sale = self.create_sale(
            quantity=2,
            unit_price="80.00",
        )
        complete_sale(sale)

        sale_item = sale.items.get()
        SaleItem.objects.filter(pk=sale_item.pk).update(
            unit_cost=None,
        )

        report = profit_and_loss(
            self.report_date,
            self.report_date,
        )

        self.assertFalse(report["costs_complete"])
        self.assertEqual(report["unknown_cost_units"], 2)
        self.assertIsNone(report["gross_profit"])
        self.assertIsNone(report["net_profit"])



# ============================================================
# SALE ADMIN PROTECTION TESTS
# ============================================================

class SaleAdminProtectionTests(CarPartsTestBase):
    """Verify completed sales are protected in Django Admin."""

    def setUp(self):
        super().setUp()

        self.admin_user = User.objects.create_superuser(
            username="sale_admin_test",
            email="sale_admin@example.com",
            password="TestPassword-2026!",
        )

        self.request = RequestFactory().get("/admin/")
        self.request.user = self.admin_user

        self.sale_admin = admin.site._registry[Sale]
        self.sale_inline = SaleItemInline(Sale, admin.site)

    def test_completed_sale_fields_are_readonly(self):
        sale = self.create_sale()
        complete_sale(sale)
        sale.refresh_from_db()

        readonly_fields = self.sale_admin.get_readonly_fields(
            self.request,
            sale,
        )

        model_field_names = {
            field.name for field in Sale._meta.fields
        }

        self.assertTrue(
            model_field_names.issubset(set(readonly_fields))
        )

    def test_completed_sale_cannot_be_deleted(self):
        sale = self.create_sale()
        complete_sale(sale)
        sale.refresh_from_db()

        self.assertFalse(
            self.sale_admin.has_delete_permission(
                self.request,
                sale,
            )
        )

    def test_completed_sale_items_cannot_be_added(self):
        sale = self.create_sale()
        complete_sale(sale)
        sale.refresh_from_db()

        self.assertFalse(
            self.sale_inline.has_add_permission(
                self.request,
                sale,
            )
        )

    def test_completed_sale_items_cannot_be_deleted(self):
        sale = self.create_sale()
        complete_sale(sale)
        sale.refresh_from_db()

        self.assertFalse(
            self.sale_inline.has_delete_permission(
                self.request,
                sale,
            )
        )

    def test_draft_sale_can_be_deleted_by_superuser(self):
        sale = self.create_sale()

        self.assertTrue(
            self.sale_admin.has_delete_permission(
                self.request,
                sale,
            )
        )

    def test_draft_sale_items_can_be_added_by_superuser(self):
        sale = self.create_sale()

        self.assertTrue(
            self.sale_inline.has_add_permission(
                self.request,
                sale,
            )
        )

    def test_draft_sale_items_can_be_deleted_by_superuser(self):
        sale = self.create_sale()

        self.assertTrue(
            self.sale_inline.has_delete_permission(
                self.request,
                sale,
            )
        )


# ============================================================
# AUTHENTICATION AND VIEW ACCESS TESTS
# ============================================================


class AuthenticationViewTests(TestCase):
    """Verify role-based access to application pages."""

    def setUp(self):
        self.regular_user = User.objects.create_user(
            username="regular_test_user",
            password="TestPassword-2026!",
        )
        self.staff_user = User.objects.create_user(
            username="staff_test_user",
            password="TestPassword-2026!",
            is_staff=True,
        )
        self.admin_user = User.objects.create_superuser(
            username="admin_test_user",
            email="admin@example.com",
            password="TestPassword-2026!",
        )

    def test_dashboard_redirects_anonymous_user_to_login(self):
        response = self.client.get(reverse("products:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_profit_loss_redirects_anonymous_user_to_login(self):
        response = self.client.get(
            reverse("products:profit_and_loss")
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/accounts/login/", response.url)

    def test_regular_user_cannot_access_dashboard(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(reverse("products:dashboard"))
        self.assertEqual(response.status_code, 302)

    def test_staff_user_can_access_dashboard(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("products:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_regular_user_cannot_access_profit_loss(self):
        self.client.force_login(self.regular_user)
        response = self.client.get(
            reverse("products:profit_and_loss")
        )
        self.assertEqual(response.status_code, 302)

    def test_staff_user_cannot_access_profit_loss(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(
            reverse("products:profit_and_loss")
        )
        self.assertEqual(response.status_code, 302)

    def test_admin_user_can_access_dashboard(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(reverse("products:dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_admin_user_can_access_profit_loss(self):
        self.client.force_login(self.admin_user)
        response = self.client.get(
            reverse("products:profit_and_loss")
        )
        self.assertEqual(response.status_code, 200)
