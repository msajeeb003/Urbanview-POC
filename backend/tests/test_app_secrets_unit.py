"""Secrets saved from the admin console (core.app_secrets): Fernet, key format rules, settings."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from pydantic import ValidationError

from core.app_secrets import (
    KEY_ADMIN,
    KEY_CHARACTERS,
    KEY_EMPTY,
    KEY_PREFIX,
    KEY_TOO_LONG,
    KEY_TOO_SHORT,
    KEY_WHITESPACE,
    SecretsUnavailable,
    SecretUnreadable,
    anthropic_key_problem,
    check_fernet_key,
    decrypt,
    encrypt,
    encryption_ready,
    last4,
    normalise_anthropic_key,
    readable,
    scrub,
)
from tests.helpers import make_settings

FAKE_KEY = "sk-ant-api03-" + "x" * 40


def _settings(key: str | None):
    return make_settings(secrets_encryption_key=key, anthropic_api_key=None)


def test_fernet_round_trip_and_failures():
    settings = _settings(Fernet.generate_key().decode())
    assert encryption_ready(settings)
    token = encrypt(settings, FAKE_KEY)
    assert token != FAKE_KEY and FAKE_KEY not in token
    assert decrypt(settings, token) == FAKE_KEY
    assert encrypt(settings, FAKE_KEY) != token  # fresh IV every time

    none = _settings(None)
    assert not encryption_ready(none)
    with pytest.raises(SecretsUnavailable):
        encrypt(none, FAKE_KEY)
    with pytest.raises(SecretsUnavailable):
        decrypt(none, token)

    other = _settings(Fernet.generate_key().decode())
    with pytest.raises(SecretUnreadable) as caught:
        decrypt(other, token)
    assert FAKE_KEY not in str(caught.value)
    assert readable(settings, token) is True
    assert readable(other, token) is False
    assert readable(none, token) is None


def test_last4_and_scrub():
    assert last4(FAKE_KEY) == "xxxx" and last4("sk-ant-abcd1234") == "1234"
    text = f"HTTP 401: invalid x-api-key {FAKE_KEY} (again: {FAKE_KEY})"
    scrubbed = scrub(text, FAKE_KEY)
    assert FAKE_KEY not in scrubbed and scrubbed.count("sk-ant-…xxxx") == 2
    assert scrub(None, FAKE_KEY) is None and scrub("plain", None) == "plain"


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", KEY_EMPTY),
        ("   \n", KEY_EMPTY),
        ("sk-ant-api03-" + "x" * 20 + " " + "x" * 20, KEY_WHITESPACE),
        ("sk-ant-api03-" + "x" * 20 + "\n" + "x" * 20, KEY_WHITESPACE),
        ("sk-proj-" + "x" * 40, KEY_PREFIX),
        ("sk-ant-admin01-" + "x" * 40, KEY_ADMIN),
        ("sk-ant-api03-short", KEY_TOO_SHORT),
        ("sk-ant-api03-" + "x" * 300, KEY_TOO_LONG),
        ("sk-ant-api03-" + "x" * 30 + "é$", KEY_CHARACTERS),
    ],
)
def test_every_key_rule_with_its_exact_message(raw, message):
    with pytest.raises(ValueError) as caught:
        normalise_anthropic_key(raw)
    assert str(caught.value) == message
    assert raw.strip() == "" or raw.strip() not in str(caught.value)


def test_messages_are_the_contract_wording():
    assert KEY_EMPTY == "Paste the API key."
    assert KEY_WHITESPACE == "The key must not contain spaces or line breaks."
    assert KEY_PREFIX == "An Anthropic API key starts with sk-ant-."
    assert KEY_ADMIN == "This is an Admin API key: paste a regular API key."
    assert KEY_TOO_SHORT == "The key is too short to be an Anthropic API key."
    assert KEY_TOO_LONG == "The key is too long to be an Anthropic API key."
    assert KEY_CHARACTERS == "The key contains characters an Anthropic API key does not use."


def test_a_valid_key_is_trimmed_and_accepted():
    assert normalise_anthropic_key(f"  {FAKE_KEY}\n") == FAKE_KEY
    long_key = "sk-ant-api03-" + "Ab9_-" * 19  # 108 characters
    assert len(long_key) == 108 and anthropic_key_problem(long_key) is None
    assert anthropic_key_problem("sk-ant-" + "x" * 33) is None  # exactly 40
    assert anthropic_key_problem("sk-ant-" + "x" * 249) is None  # exactly 256
    assert anthropic_key_problem("sk-ant-" + "x" * 250) == KEY_TOO_LONG


def test_fernet_key_check():
    check_fernet_key(Fernet.generate_key().decode())
    check_fernet_key("  " + Fernet.generate_key().decode() + " ")
    for bad in ("not-a-key", "x" * 44, "é" * 44, ""):
        with pytest.raises(ValueError) as caught:
            check_fernet_key(bad)
        assert "Fernet.generate_key" in str(caught.value)
        if bad:
            assert bad not in str(caught.value)


def test_settings_validate_the_encryption_key_without_echoing_it():
    bad = "definitely-not-a-fernet-key-" + "y" * 16
    with pytest.raises(ValidationError) as caught:
        make_settings(secrets_encryption_key=bad)
    assert bad not in str(caught.value)
    good = Fernet.generate_key().decode()
    settings = make_settings(secrets_encryption_key=good)
    assert settings.secrets_encryption_key.get_secret_value() == good


def test_blank_env_values_are_unset(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", "  ")
    settings = make_settings()
    assert settings.anthropic_api_key is None
    assert settings.secrets_encryption_key is None
