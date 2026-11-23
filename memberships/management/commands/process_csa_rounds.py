import logging

from django.core.management.base import BaseCommand, CommandError

from memberships.csa_rounds import process_due_rounds, process_round_recurrences

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = (
        "Close CSA selections, expire reselections, refresh membership status "
        "and generate recurring CSA rounds."
    )

    def handle(self, *args, **options):
        failures = []
        try:
            generated, failed = process_round_recurrences()
        except Exception:
            logger.exception("Could not complete CSA recurrence generation")
            failures.append("CSA recurrence generation")
        else:
            self.stdout.write(f"Generated {generated} recurring CSA rounds.")
            if failed:
                failures.append(f"{failed} CSA recurrence configuration(s)")

        try:
            count = process_due_rounds()
        except Exception:
            logger.exception("Could not complete CSA selection reconciliation")
            failures.append("CSA selection reconciliation")
        else:
            self.stdout.write(f"Processed {count} CSA rounds.")

        if failures:
            raise CommandError("Failed to process " + ", ".join(failures))
