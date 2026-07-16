import pytest
from django.core.exceptions import ImproperlyConfigured

from config.db.backends.mysql_connector.base import DatabaseWrapper
from config.settings import env_bool


def test_env_bool_rejects_typo(monkeypatch):
    monkeypatch.setenv("TEST_BOOLEAN", "treu")

    with pytest.raises(ImproperlyConfigured, match="must be a boolean"):
        env_bool("TEST_BOOLEAN")


def test_mysql_connector_display_name_is_django_compatible():
    wrapper = object.__new__(DatabaseWrapper)

    assert wrapper.display_name == "MySQL"
