"""Report effective legacy reservations that need attention before rollout."""

import json
from itertools import islice

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from commerce.inventory import effective_store_stock_reservations, inventory_capacity
from memberships.csa_rounds import effective_stock_hold_rows


class Command(BaseCommand):
    help = "Check whether existing unassigned Store and CSA stock holds can fit on single Farms."

    def handle(self, *args, **options):
        now = timezone.now()
        store = effective_store_stock_reservations(now=now)
        csa = effective_stock_hold_rows(now=now)
        products = (
            store.filter(farm_id__isnull=True).order_by().values_list("product_id", flat=True)
        )
        csa_products = (
            csa.filter(farm_id__isnull=True).order_by().values_list("product_id", flat=True)
        )
        product_ids = products.union(csa_products).order_by("product_id").iterator(chunk_size=250)
        checked = blocked = holds = 0
        while batch := tuple(islice(product_ids, 250)):
            capacity = inventory_capacity(batch, now=now)
            checked += len(capacity.legacy)
            holds += sum(capacity.legacy_hold_counts.values())
            for product_id in sorted(capacity.legacy_shortfalls):
                blocked += 1
                self.stdout.write(
                    json.dumps(
                        {
                            "product_id": str(product_id),
                            "unassigned_holds": capacity.legacy_hold_counts[product_id],
                            "unplaced_hold": capacity.legacy_shortfalls.get(product_id),
                        }
                    )
                )
        self.stdout.write(
            json.dumps({"checked_products": checked, "unassigned_holds": holds, "blocked": blocked})
        )
        if blocked:
            raise CommandError("Some effective legacy holds have no verified single-Farm plan.")
