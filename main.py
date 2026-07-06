"""Runnable entrypoint: wire the adapters + orchestrator from config, run the pipeline.

Turns the parts into a service. Two modes:
  single-poll  process the current inbox once and exit
  loop         poll every POLL_INTERVAL_SECONDS until interrupted (Ctrl-C / SIGTERM)

Usage:
    python main.py single-poll
    python main.py loop

Config comes from `.env` (see `.env.example`).

Sink and resolver are pluggable. This repo ships the generic pipeline — Gmail
inbox, Claude extraction, Gmail review queue — plus stub sink/resolver so the
service runs end-to-end out of the box. To file expenses into a real system,
implement `ExpenseSink` and `JobResolver` (see `core/ports.py`) and wire them
in `build_pipeline`. The first production deployment targets a private
job-costing system through exactly this seam; an adapter that submits
candidates to the Bookkeeper UI's ingest port is planned (see the issue
tracker).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from config import Config, ConfigError, load_config
from adapters.claude_extractor import ClaudeExtractor
from adapters.gmail_inbox import GmailInbox
from adapters.gmail_review_queue import GmailReviewQueue
from adapters.stubs import StubExpenseSink, StubJobResolver
from core.orchestrator import Orchestrator

logger = logging.getLogger("receipt_intake")

# Gmail label applied to items routed for human review (see docs/DECISIONS.md).
REVIEW_LABEL = "needs-review"

# Vars the service needs beyond config's gmail-scoped _REQUIRED. Checked at
# startup so a misconfigured deploy fails fast with a clear, secret-free message.
_SERVICE_REQUIRED = {
    "anthropic_api_key": "ANTHROPIC_API_KEY",
}


def build_pipeline(cfg: Config) -> Orchestrator:
    """Construct the adapters + orchestrator from config.

    The inbox, extractor, and review queue are the real generic adapters.
    The sink and resolver default to the stubs (log-and-collect) — replace
    them with your target-system adapters to file expenses somewhere real.
    """
    inbox = GmailInbox(
        intake_address=cfg.gmail_intake_address,
        oauth_client_path=cfg.gmail_oauth_client_path,
        token_path=cfg.gmail_token_path,
        processed_label=cfg.gmail_processed_label,
    )
    extractor = ClaudeExtractor(api_key=cfg.anthropic_api_key)
    job_resolver = StubJobResolver()
    sink = StubExpenseSink()
    review_queue = GmailReviewQueue(
        token_path=cfg.gmail_token_path,
        review_label=REVIEW_LABEL,
        notify_email=cfg.notify_email,
    )
    return Orchestrator(
        inbox=inbox,
        extractor=extractor,
        job_resolver=job_resolver,
        sink=sink,
        review_queue=review_queue,
    )


def parse_poll_interval(cfg: Config) -> int:
    """Parse POLL_INTERVAL_SECONDS to a positive int, or raise ConfigError."""
    raw = cfg.poll_interval_seconds
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        raise ConfigError(
            f"POLL_INTERVAL_SECONDS must be an integer (got {raw!r})"
        )
    if seconds <= 0:
        raise ConfigError(
            f"POLL_INTERVAL_SECONDS must be positive (got {seconds})"
        )
    return seconds


def check_service_config(cfg: Config) -> None:
    """Fail fast (secret-free) if vars the running service needs are unset."""
    missing = [
        env_name
        for field, env_name in _SERVICE_REQUIRED.items()
        if not str(getattr(cfg, field, "")).strip()
    ]
    if missing:
        raise ConfigError(
            "Missing required config to run the service (set in .env): "
            + ", ".join(missing)
        )


def startup_summary(cfg: Config, mode: str) -> str:
    """Build the startup banner — names only, never secrets."""
    lines = [
        "receipt-intake starting",
        f"  mode:    {mode}",
        f"  intake:  {cfg.gmail_intake_address}",
        f"  sink:    stub (wire your ExpenseSink in build_pipeline)",
        f"  review:  label '{REVIEW_LABEL}'",
    ]
    if mode == "loop":
        lines.append(f"  poll:    every {parse_poll_interval(cfg)}s")
    return "\n".join(lines)


async def run_once(orchestrator: Orchestrator) -> None:
    """Process the current inbox once."""
    await orchestrator.run()


async def run_loop(
    orchestrator: Orchestrator,
    interval_seconds: int,
    stop_event: asyncio.Event,
) -> None:
    """Poll repeatedly until stop_event is set, sleeping interval between polls.

    The sleep is interruptible: a shutdown signal wakes it immediately rather
    than waiting out the full interval.
    """
    while not stop_event.is_set():
        await orchestrator.run()
        if stop_event.is_set():
            break
        try:
            # Sleep until the interval elapses OR a shutdown signal arrives.
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except asyncio.TimeoutError:
            pass  # interval elapsed — poll again


async def run_service(mode: str, cfg: Config) -> None:
    """Build the pipeline, install signal handlers, run the chosen mode."""
    check_service_config(cfg)
    orchestrator = build_pipeline(cfg)

    logger.info(startup_summary(cfg, mode))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Signal handlers aren't available on some platforms (e.g. Windows);
            # the KeyboardInterrupt fallback in main() still exits cleanly.
            pass

    if mode == "single-poll":
        await run_once(orchestrator)
    else:
        await run_loop(orchestrator, parse_poll_interval(cfg), stop_event)

    # Adapters that own external resources (DB pools, HTTP sessions) close them
    # here. The shipped stub sink/resolver hold nothing; a real sink adapter
    # should expose and receive a close()/shutdown hook (see docs/DECISIONS.md
    # on pool handling in the first production deployment).


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Run the receipt-intake pipeline as a service."
    )
    parser.add_argument(
        "mode",
        choices=["single-poll", "loop"],
        help="single-poll: process the inbox once and exit. "
        "loop: poll every POLL_INTERVAL_SECONDS until interrupted.",
    )
    args = parser.parse_args()

    cfg = load_config()

    try:
        asyncio.run(run_service(args.mode, cfg))
    except KeyboardInterrupt:
        logger.info("Interrupted — exiting")


if __name__ == "__main__":
    main()
