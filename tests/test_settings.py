from types import SimpleNamespace

import pytest
from django.core.exceptions import ImproperlyConfigured
from mysql.connector.conversion import MySQLConverter

from config.db.backends.mysql_connector.base import DatabaseWrapper
from config.db.backends.mysql_connector.schema import DatabaseSchemaEditor
from config.settings import env_bool


def test_env_bool_rejects_typo(monkeypatch):
    monkeypatch.setenv("TEST_BOOLEAN", "treu")

    with pytest.raises(ImproperlyConfigured, match="must be a boolean"):
        env_bool("TEST_BOOLEAN")


def test_mysql_connector_display_name_is_django_compatible():
    wrapper = object.__new__(DatabaseWrapper)

    assert wrapper.display_name == "MySQL"


def test_mysql_connector_schema_literals_are_quoted():
    editor = object.__new__(DatabaseSchemaEditor)
    editor.connection = SimpleNamespace(
        ensure_connection=lambda: None,
        connection=SimpleNamespace(converter=MySQLConverter()),
    )

    assert editor.quote_value("pending_verification") == "'pending_verification'"
    assert editor.quote_value(True) == "1"
