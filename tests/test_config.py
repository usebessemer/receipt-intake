"""Tests for the config loader.

Run from repo root:  pytest
These tests never touch a real .env — they point load_config() at a temp file
and/or set os.environ directly, then restore the environment afterward.
"""

import pytest

from config import Config, ConfigError, load_config

# The currently required vars (Gmail foundation only).
_REQUIRED_ENV = {
    "GMAIL_INTAKE_ADDRESS": "receipts@example.com",
    "GMAIL_OAUTH_CLIENT_PATH": "./secrets/gmail_oauth_client.json",
    "GMAIL_TOKEN_PATH": "./secrets/gmail_token.json",
}

# All env vars the loader knows about — cleared before each test for isolation.
_ALL_ENV = _REQUIRED_ENV | {
    "GMAIL_PROCESSED_LABEL": "",
    "ANTHROPIC_API_KEY": "",
    "EXTRACTION_MODEL": "",
    "POLL_INTERVAL_SECONDS": "",
    "NOTIFY_EMAIL": "",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Start every test from a clean slate — no inherited config vars."""
    for key in _ALL_ENV:
        monkeypatch.delenv(key, raising=False)
    yield


def _set(monkeypatch, env: dict):
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_loads_all_required_from_env(monkeypatch, tmp_path):
    _set(monkeypatch, _REQUIRED_ENV)
    cfg = load_config(env_file=tmp_path / "absent.env")

    assert isinstance(cfg, Config)
    assert cfg.gmail_intake_address == "receipts@example.com"
    assert cfg.gmail_oauth_client_path == "./secrets/gmail_oauth_client.json"
    assert cfg.gmail_token_path == "./secrets/gmail_token.json"


def test_defaults_applied_when_optional_unset(monkeypatch, tmp_path):
    _set(monkeypatch, _REQUIRED_ENV)
    cfg = load_config(env_file=tmp_path / "absent.env")

    assert cfg.gmail_processed_label == "processed"
    assert cfg.extraction_model == "claude-haiku-4-5-20251001"
    assert cfg.poll_interval_seconds == "300"
    assert cfg.notify_email == ""


def test_missing_required_raises_listing_all(monkeypatch, tmp_path):
    # Provide only one required var; expect the other two reported together.
    _set(monkeypatch, {"GMAIL_INTAKE_ADDRESS": "receipts@example.com"})

    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=tmp_path / "absent.env")

    msg = str(excinfo.value)
    assert "GMAIL_OAUTH_CLIENT_PATH" in msg
    assert "GMAIL_TOKEN_PATH" in msg
    # The one we did supply must NOT be reported as missing.
    assert "GMAIL_INTAKE_ADDRESS" not in msg


def test_blank_and_whitespace_treated_as_missing(monkeypatch, tmp_path):
    env = dict(_REQUIRED_ENV)
    env["GMAIL_TOKEN_PATH"] = "   "  # whitespace-only = unset
    _set(monkeypatch, env)

    with pytest.raises(ConfigError) as excinfo:
        load_config(env_file=tmp_path / "absent.env")
    assert "GMAIL_TOKEN_PATH" in str(excinfo.value)


def test_values_are_stripped(monkeypatch, tmp_path):
    env = dict(_REQUIRED_ENV)
    env["GMAIL_INTAKE_ADDRESS"] = "  receipts@example.com  "
    _set(monkeypatch, env)

    cfg = load_config(env_file=tmp_path / "absent.env")
    assert cfg.gmail_intake_address == "receipts@example.com"


def test_reads_from_env_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in _REQUIRED_ENV.items()) + "\n"
    )

    cfg = load_config(env_file=env_file)
    assert cfg.gmail_intake_address == "receipts@example.com"


def test_real_env_overrides_file(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(f"{k}={v}" for k, v in _REQUIRED_ENV.items()) + "\n"
    )
    # A real env var should win over the file value (deploy-injected secret).
    monkeypatch.setenv("GMAIL_INTAKE_ADDRESS", "override@example.com")

    cfg = load_config(env_file=env_file)
    assert cfg.gmail_intake_address == "override@example.com"


def test_config_is_immutable(monkeypatch, tmp_path):
    _set(monkeypatch, _REQUIRED_ENV)
    cfg = load_config(env_file=tmp_path / "absent.env")

    with pytest.raises(Exception):
        cfg.gmail_intake_address = "mutated@example.com"  # type: ignore[misc]
