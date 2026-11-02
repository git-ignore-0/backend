from types import SimpleNamespace

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from mysql.connector.conversion import MySQLConverter

from config.db.backends.mysql_connector.base import DatabaseWrapper
from config.db.backends.mysql_connector.schema import DatabaseSchemaEditor
from config.settings import env_bool, parse_origins


def test_env_bool_rejects_typo(monkeypatch):
    monkeypatch.setenv("TEST_BOOLEAN", "treu")

    with pytest.raises(ImproperlyConfigured, match="must be a boolean"):
        env_bool("TEST_BOOLEAN")


def test_parse_origins_normalizes_and_deduplicates_origins():
    assert parse_origins(
        "TEST_ORIGINS",
        "https://NaturalFarmingVietnam.com/, http://localhost:3000,"
        "https://naturalfarmingvietnam.com",
    ) == (
        "https://naturalfarmingvietnam.com",
        "http://localhost:3000",
    )


@pytest.mark.parametrize(
    "value",
    [
        "ftp://example.com",
        "https://example.com/path",
        "https://user:password@example.com",
        "https://example.com?next=/account",
        "https://example.com:not-a-port",
    ],
)
def test_parse_origins_rejects_non_origin_values(value):
    with pytest.raises(ImproperlyConfigured, match="HTTP\\(S\\) origins"):
        parse_origins("TEST_ORIGINS", value)


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


def test_cross_origin_oauth_popup_keeps_window_opener(client):
    assert settings.SECURE_CROSS_ORIGIN_OPENER_POLICY is None

    response = client.get("/login")

    assert "Cross-Origin-Opener-Policy" not in response


def test_mysql_store_transactions_use_read_committed(monkeypatch):
    import runpy
    from unittest.mock import MagicMock

    for name, value in {
        "DB_ENGINE": "mysql",
        "DB_NAME": "test",
        "DB_USER": "test",
        "DB_PASSWORD": "test",
    }.items():
        monkeypatch.setenv(name, value)
    namespace = runpy.run_path(str(settings.BASE_DIR / "config/settings.py"))
    configured = namespace["DATABASES"]["default"]
    wrapper = DatabaseWrapper(configured, alias="isolation-check")
    wrapper.get_connection_params()
    assert wrapper.isolation_level == "read committed"
    wrapper.features = SimpleNamespace(is_sql_auto_is_null_enabled=False)
    cursor = MagicMock()
    wrapper.cursor = MagicMock(return_value=cursor)
    wrapper.init_connection_state()
    cursor.__enter__.return_value.execute.assert_called_once_with(
        "SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED"
    )


@pytest.mark.parametrize("enabled,want", [(None, False), ("false", False), ("true", True)])
def test_store_referral_settings_need_only_toggle(monkeypatch, enabled, want):
    import runpy

    # Only the environment selected by this test may define the toggle.
    monkeypatch.setattr("dotenv.load_dotenv", lambda *_args, **_kwargs: False)

    if enabled is None:
        monkeypatch.delenv("STORE_REFERRAL_REWARDS_ENABLED", raising=False)
    else:
        monkeypatch.setenv("STORE_REFERRAL_REWARDS_ENABLED", enabled)
    monkeypatch.delenv("STORE_REFERRAL_REWARDS_START_AT", raising=False)
    namespace = runpy.run_path(str(settings.BASE_DIR / "config/settings.py"))
    assert namespace["STORE_REFERRAL_REWARDS_ENABLED"] is want
