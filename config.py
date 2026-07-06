"""Centralized config + secrets loader for receipt-intake.

Every setting comes from `.env` (see `.env.example`) — nothing is hardcoded.
The intake email account is a config value (`GMAIL_INTAKE_ADDRESS`), so moving
inboxes is a config change, not a code change.

Usage:
    from config import load_config
    cfg = load_config()          # reads .env, validates, returns a frozen Config
    print(cfg.gmail_intake_address)

`load_config()` raises `ConfigError` listing *every* missing required var at
once, so a misconfigured deploy fails fast at startup with one clear message
rather than crashing deep in the pipeline ("fail safe, never silent").
"""

from __future__ import annotations

import os
from dataclasses import MISSING, dataclass, fields
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

# Where this module lives — used to find the module-local .env by default.
_MODULE_DIR = Path(__file__).resolve().parent


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Typed, immutable view of all receipt-intake settings.

    Field order / names mirror `.env.example` so the two stay easy to diff.
    Fields with a default here are optional in `.env`; everything else is
    required and validated by `load_config()`.

    Sink/resolver adapters own their own settings — a target-system adapter
    should read its connection config where it is wired (see `main.py`),
    keeping this core config target-agnostic.
    """

    # --- Intake email (SWAPPABLE) ---
    gmail_intake_address: str
    gmail_oauth_client_path: str
    gmail_token_path: str
    gmail_processed_label: str = "processed"

    # --- Anthropic (extraction) ---
    anthropic_api_key: str = ""
    extraction_model: str = "claude-haiku-4-5-20251001"

    # --- Service (entrypoint) ---
    # Seconds between polls in loop mode. Stored as a string to match the
    # all-string loader; parsed to int at the entrypoint.
    poll_interval_seconds: str = "300"

    # --- Notifications ---
    notify_email: str = ""


# Maps each Config field -> its .env variable name. Single source of truth for
# both reading values and reporting which env var is missing.
_ENV_VARS: dict[str, str] = {
    "gmail_intake_address": "GMAIL_INTAKE_ADDRESS",
    "gmail_oauth_client_path": "GMAIL_OAUTH_CLIENT_PATH",
    "gmail_token_path": "GMAIL_TOKEN_PATH",
    "gmail_processed_label": "GMAIL_PROCESSED_LABEL",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "extraction_model": "EXTRACTION_MODEL",
    "poll_interval_seconds": "POLL_INTERVAL_SECONDS",
    "notify_email": "NOTIFY_EMAIL",
}

# Required at startup — the Gmail foundation. The service entrypoint checks
# its own additional requirements (e.g. ANTHROPIC_API_KEY) at startup.
_REQUIRED: tuple[str, ...] = (
    "gmail_intake_address",
    "gmail_oauth_client_path",
    "gmail_token_path",
)


def load_config(env_file: Optional[os.PathLike[str] | str] = None) -> Config:
    """Load, validate, and return the receipt-intake config.

    Reads `.env` (module-local by default; pass `env_file` to override).
    Existing real environment variables take precedence over `.env` values,
    which lets deploys (cron, CI) inject secrets without a file on disk.

    Raises `ConfigError` if any required var is missing or blank — the message
    lists all of them at once.
    """
    path = Path(env_file) if env_file is not None else _MODULE_DIR / ".env"
    # override=False: real env wins over the file. load_dotenv silently no-ops
    # if the file is absent, which is the intended "env-only" deploy path.
    load_dotenv(dotenv_path=path, override=False)

    # Defaults declared on the dataclass; used when a var is absent/blank.
    # Fields with no declared default (the required ones) fall back to "" so a
    # missing var becomes a blank we can detect below, not a MISSING sentinel.
    defaults = {
        f.name: ("" if f.default is MISSING else f.default) for f in fields(Config)
    }

    values: dict[str, str] = {}
    for field_name, env_name in _ENV_VARS.items():
        raw = os.environ.get(env_name)
        # Treat blank/whitespace as unset so an empty line in .env doesn't
        # masquerade as a configured value.
        if raw is None or raw.strip() == "":
            values[field_name] = defaults[field_name]
        else:
            values[field_name] = raw.strip()

    missing = [
        _ENV_VARS[name] for name in _REQUIRED if not str(values.get(name, "")).strip()
    ]
    if missing:
        raise ConfigError(
            "Missing required config (set these in .env — see .env.example): "
            + ", ".join(missing)
        )

    return Config(**values)
