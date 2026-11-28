"""Report effective holds that need recovery or rollout attention."""

import json
from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from commerce.inventory import effective_store_stock_reservations, inventory_capacity
from memberships.csa_rounds import effective_stock_hold_rows


class Command(BaseCommand):
    help = "Check single-Farm coverage for effective Store and CSA stock holds."

    def handle(self, *args, **options):
        now = timezone.now()
        store = effective_store_stock_reservations(now=now)
        csa = effective_stock_hold_rows(now=now)
        products = store.order_by().values_list("product_id", flat=True)
        csa_products = csa.order_by().values_list("product_id", flat=True)
        product_ids = products.union(csa_products).order_by("product_id").iterator(chunk_size=250)
        checked = blocked = unassigned = stranded = 0
        while batch := tuple(islice(product_ids, 250)):
            capacity = inventory_capacity(batch, now=now)
            checked += len(capacity.total_demand)
            unassigned += sum(capacity.legacy_hold_counts.values())
            stranded += sum(capacity.stranded_hold_counts.values())
            blocked += len(capacity.recovery_shortfalls)
            for product_id in sorted(
                capacity.recovery_shortfalls.keys() | capacity.stranded_hold_counts.keys()
            ):
                self.stdout.write(
                    json.dumps(
                        {
                            "product_id": str(product_id),
                            "unassigned_holds": capacity.legacy_hold_counts.get(product_id, 0),
                            "stranded_holds": capacity.stranded_hold_counts.get(product_id, 0),
                            "unplaced_hold": capacity.recovery_shortfalls.get(product_id),
                            "remaining_capacity": capacity.maximum(product_id),
                        }
                    )
                )
        self.stdout.write(
            json.dumps(
                {
                    "checked_products": checked,
                    "unassigned_holds": unassigned,
                    "stranded_holds": stranded,
                    "blocked": blocked,
                }
            )
        )
        if blocked:
            raise CommandError("Some effective holds have no verified single-Farm plan.")
