from typing import Any

from mysql.connector.django.schema import DatabaseSchemaEditor as ConnectorSchemaEditor


class DatabaseSchemaEditor(ConnectorSchemaEditor):
    """Quote schema literals correctly for constraints and field defaults."""

    def quote_value(self, value: Any) -> str:
        self.connection.ensure_connection()
        converter = self.connection.connection.converter
        if isinstance(value, str):
            value = value.replace("%", "%%")
        prepared = converter.to_mysql(value)
        escaped = converter.escape(prepared)
        quoted = converter.quote(escaped)
        if isinstance(quoted, (bytes, bytearray)):
            return quoted.decode()
        return str(quoted)
