from django.core.management.base import BaseCommand

from memberships.csa_rounds import process_due_rounds


class Command(BaseCommand):
    help = "Close CSA selections, expire reselections and refresh membership frozen status."

    def handle(self, *args, **options):
        count = process_due_rounds()
        self.stdout.write(f"Processed {count} CSA rounds.")
