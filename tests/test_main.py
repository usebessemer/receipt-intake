"""Tests for the service entrypoint (main.py) — hermetic, no network/DB."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import main
from config import Config, ConfigError
from core.orchestrator import Orchestrator
from adapters.claude_extractor import ClaudeExtractor
from adapters.gmail_inbox import GmailInbox
from adapters.gmail_review_queue import GmailReviewQueue
from adapters.stubs import StubExpenseSink, StubJobResolver


def _config(**overrides) -> Config:
    """Build a Config with safe test defaults; override per test."""
    base = dict(
        gmail_intake_address="receipts@example.com",
        gmail_oauth_client_path="./secrets/oauth.json",
        gmail_token_path="./secrets/token.json",
        gmail_processed_label="processed",
        anthropic_api_key="sk-ant-test",
        poll_interval_seconds="300",
        notify_email="notify@example.com",
    )
    base.update(overrides)
    return Config(**base)


# --- parse_poll_interval ---

def test_parse_poll_interval_valid():
    assert main.parse_poll_interval(_config(poll_interval_seconds="120")) == 120


def test_parse_poll_interval_non_numeric_raises():
    with pytest.raises(ConfigError):
        main.parse_poll_interval(_config(poll_interval_seconds="abc"))


def test_parse_poll_interval_non_positive_raises():
    with pytest.raises(ConfigError):
        main.parse_poll_interval(_config(poll_interval_seconds="0"))


# --- check_service_config ---

def test_check_service_config_ok():
    main.check_service_config(_config())  # should not raise


def test_check_service_config_missing_api_key():
    with pytest.raises(ConfigError) as exc:
        main.check_service_config(_config(anthropic_api_key=""))
    assert "ANTHROPIC_API_KEY" in str(exc.value)


# --- startup_summary: names only, never secrets ---

def test_startup_summary_has_names_not_secrets():
    cfg = _config()
    summary = main.startup_summary(cfg, "loop")
    assert "receipts@example.com" in summary
    assert "every 300s" in summary
    # Secrets must never appear in the banner
    assert "sk-ant-test" not in summary


def test_startup_summary_single_poll_omits_interval():
    summary = main.startup_summary(_config(), "single-poll")
    assert "poll:" not in summary


# --- build_pipeline: correct wiring, hermetic ---

def test_build_pipeline_wires_all_adapters():
    orchestrator = main.build_pipeline(_config())

    assert isinstance(orchestrator, Orchestrator)
    # Orchestrator wired with the real generic adapters + the stub seam
    assert isinstance(orchestrator.inbox, GmailInbox)
    assert isinstance(orchestrator.extractor, ClaudeExtractor)
    assert isinstance(orchestrator.job_resolver, StubJobResolver)
    assert isinstance(orchestrator.sink, StubExpenseSink)
    assert isinstance(orchestrator.review_queue, GmailReviewQueue)


# --- run_once / run_loop ---

async def test_run_once_calls_run():
    orchestrator = AsyncMock()
    await main.run_once(orchestrator)
    orchestrator.run.assert_awaited_once()


async def test_run_loop_stops_when_event_set():
    orchestrator = AsyncMock()
    stop_event = asyncio.Event()

    # Stop after the first poll by setting the event from within run()
    async def run_then_stop():
        stop_event.set()

    orchestrator.run.side_effect = run_then_stop

    await main.run_loop(orchestrator, interval_seconds=999, stop_event=stop_event)

    orchestrator.run.assert_awaited_once()


async def test_run_loop_polls_until_stopped():
    orchestrator = AsyncMock()
    stop_event = asyncio.Event()
    calls = {"n": 0}

    async def count_then_stop():
        calls["n"] += 1
        if calls["n"] >= 3:
            stop_event.set()

    orchestrator.run.side_effect = count_then_stop

    # Tiny interval so the interruptible sleep returns fast between polls
    await main.run_loop(orchestrator, interval_seconds=0.01, stop_event=stop_event)

    assert calls["n"] == 3
