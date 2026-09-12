from django.core.management.base import BaseCommand, CommandError

from memberships.expiry import (
    DEFAULT_BATCH_SIZE,
    MAX_BATCH_SIZE,
    expire_csa_purchase_requests,
)


class Command(BaseCommand):
    help = "Expire overdue open CSA purchase requests."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= MAX_BATCH_SIZE:
            raise CommandError(f"--batch-size must be between 1 and {MAX_BATCH_SIZE}")
        dry_run = options["dry_run"]
        count = expire_csa_purchase_requests(
            batch_size=batch_size,
            dry_run=dry_run,
        )
        verb = "Would expire" if dry_run else "Expired"
        self.stdout.write(f"{verb} {count} CSA purchase request(s).")
