from django.core.management.base import BaseCommand, CommandError
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.loader import MigrationLoader

from farms.migrations._lifecycle_preflight import (
    LEGACY_TARGETS,
    LIFECYCLE_MIGRATIONS,
    FarmProductLifecyclePreflightError,
    validate_lifecycle_preflight,
)


class Command(BaseCommand):
    help = "Validate FarmProduct lifecycle cutover prerequisites on the selected database."

    def add_arguments(self, parser):
        parser.add_argument("--database", choices=tuple(connections), default=DEFAULT_DB_ALIAS)

    def handle(self, *args, database, **options):
        connection = connections[database]
        try:
            loader = MigrationLoader(connection)
            loader.check_consistent_history(connection)
            targets = [*LEGACY_TARGETS]
            targets.extend(
                node for node in LIFECYCLE_MIGRATIONS if node in loader.applied_migrations
            )
            state = loader.project_state(targets)
            validate_lifecycle_preflight(state.apps, connection)
        except (
            DatabaseError,
            InconsistentMigrationHistory,
            FarmProductLifecyclePreflightError,
        ) as error:
            raise CommandError(
                f"Database {database} failed lifecycle preflight. {error}"
            ) from error

        self.stdout.write(
            f"Database {database} passed lifecycle preflight. Repeat under the cutover write fence."
        )
