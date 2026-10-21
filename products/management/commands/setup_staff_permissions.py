
from django.contrib.auth.models import Group, Permission
from django.core.management.base import BaseCommand
from django.db import transaction


class Command(BaseCommand):
    help = "Create and configure the Staff group permissions."

    @transaction.atomic
    def handle(self, *args, **options):
        staff_group, created = Group.objects.get_or_create(
            name="Staff"
        )

        # Clear old permissions so rerunning this command
        # always produces the intended permission set.
        staff_group.permissions.clear()


        allowed_permissions = {
            # View core business records.
            "view_brand",
            "view_category",
            "view_customer",
            "view_product",
            "view_supplier",
            "view_vehiclemodel",
            "view_sale",
            "view_saleitem",
            "view_payment",
            "view_inventorytransaction",

            # Record payments using the dedicated workflow.
            "add_payment",
            "add_sale",
            "add_saleitem",
            "change_product",
            # View purchasing records.
            "view_purchase",
            "view_purchaseitem",
        }


        permissions = Permission.objects.filter(
            content_type__app_label="products",
            codename__in=allowed_permissions,
        )

        found_permissions = set(
            permissions.values_list("codename", flat=True)
        )

        missing_permissions = allowed_permissions - found_permissions

        if missing_permissions:
            self.stdout.write(
                self.style.WARNING(
                    "Some expected permissions were not found: "
                    + ", ".join(sorted(missing_permissions))
                )
            )

        staff_group.permissions.set(permissions)

        action = "Created" if created else "Updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"{action} Staff group with "
                f"{permissions.count()} permissions."
            )
        )
