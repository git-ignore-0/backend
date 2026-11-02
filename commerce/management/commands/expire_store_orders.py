from django.core.management.base import BaseCommand

from commerce.store_orders import expire_store_orders


class Command(BaseCommand):
    help = "Cancel expired Store checkouts/orders and release stock/quota reservations."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=200)

    def handle(self, *args, **options):
        count = expire_store_orders(limit=max(1, options["batch_size"]))
        self.stdout.write(f"Expired {count} Store order(s).")
