from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.config import AppEnv, Settings


def test_lists_accept_csv_and_json(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "http://a.example, http://b.example")
    monkeypatch.setenv("RATE_LIMIT_EXEMPT_PATHS", '["/health", "/metrics"]')
    s = Settings(_env_file=None)
    assert s.cors_origins == ["http://a.example", "http://b.example"]
    assert s.rate_limit_exempt_paths == ["/health", "/metrics"]


def test_env_selects_environment(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal:5432/urbanview")
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "hunter2-value")
    s = Settings(_env_file=None)
    assert s.app_env is AppEnv.staging
    assert not s.is_dev and not s.is_prod
    assert s.s3_secret_key.get_secret_value() == "hunter2-value"
    assert "hunter2-value" not in repr(s)  # SecretStr masks the value in repr/logs


def test_non_dev_rejects_dev_defaults(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
    monkeypatch.delenv("S3_SECRET_KEY", raising=False)
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, app_env="prod")
    message = str(excinfo.value)
    assert "DATABASE_URL" in message
    assert "S3_ACCESS_KEY" in message


def test_non_dev_rejects_short_admin_tokens(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@db.internal:5432/urbanview")
    monkeypatch.setenv("S3_ACCESS_KEY", "key")
    monkeypatch.setenv("S3_SECRET_KEY", "secret-value")
    with pytest.raises(ValidationError, match="at least 24 characters"):
        Settings(_env_file=None, app_env="staging", admin_api_tokens="change-me:admin:ops")
    good = Settings(_env_file=None, app_env="staging", admin_api_tokens=f"{'a' * 32}:admin:ops")
    assert good.admin_api_tokens is not None
    # dev keeps short tokens for local work and tests
    assert Settings(_env_file=None, admin_api_tokens="dev:admin").admin_api_tokens is not None


def test_rate_limit_values_are_validated():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rate_limit_requests=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, rate_limit_window_seconds=0)
