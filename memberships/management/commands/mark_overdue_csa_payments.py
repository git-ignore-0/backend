from django.core.management.base import BaseCommand, CommandError

from memberships.payments import mark_overdue_csa_payments


class Command(BaseCommand):
    help = "Mark pending CSA installments past their due time as overdue."

    def add_arguments(self, parser):
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        batch_size = options["batch_size"]
        if not 1 <= batch_size <= 1000:
            raise CommandError("--batch-size must be between 1 and 1000")
        count = mark_overdue_csa_payments(batch_size=batch_size)
        self.stdout.write(f"Marked {count} CSA payment(s) overdue.")
