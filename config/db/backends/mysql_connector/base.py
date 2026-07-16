from django.utils.functional import cached_property
from mysql.connector.django.base import DatabaseWrapper as ConnectorDatabaseWrapper


class DatabaseWrapper(ConnectorDatabaseWrapper):
    """Compatibility fixes for MySQL Connector/Python's Django backend."""

    @cached_property
    def display_name(self) -> str:
        return "MySQL"
