from django.utils.functional import cached_property
from mysql.connector.django.base import DatabaseWrapper as ConnectorDatabaseWrapper

from config.db.backends.mysql_connector.schema import DatabaseSchemaEditor


class DatabaseWrapper(ConnectorDatabaseWrapper):
    """Compatibility fixes for MySQL Connector/Python's Django backend."""

    SchemaEditorClass = DatabaseSchemaEditor

    @cached_property
    def display_name(self) -> str:
        return "MySQL"
