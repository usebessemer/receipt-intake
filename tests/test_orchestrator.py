"""Tests for the core orchestrator with stub adapters.

Proves the end-to-end pipeline works: Inbox → Extract → Resolve → Store.
Also tests failure paths: extraction failure and unmatched jobs route to review.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from adapters.stubs import (
    StubExpenseSink,
    StubExtractor,
    StubInboxSource,
    StubJobResolver,
    StubReviewQueue,
)
from core.orchestrator import Orchestrator
from core.ports import ExpenseKind, InboxItem


async def test_orchestrator_processes_item_end_to_end():
    """Exercise the full pipeline: inbox → extract → resolve → store."""
    # Set up stubs with a test receipt
    inbox = StubInboxSource(
        items=[
            InboxItem(
                inbox_id="test-001",
                subject="Acme Supplies, May 15",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc),
                image_bytes=b"fake receipt image",
            )
        ]
    )
    extractor = StubExtractor()
    resolver = StubJobResolver()
    sink = StubExpenseSink()
    review_queue = StubReviewQueue()

    # Wire and run the orchestrator
    orchestrator = Orchestrator(
        inbox=inbox,
        extractor=extractor,
        job_resolver=resolver,
        sink=sink,
        review_queue=review_queue,
    )
    await orchestrator.run()

    # Verify: item was stored and marked processed
    assert len(sink.stored) == 1
    expense = sink.stored[0]
    assert expense.merchant == "Office Depot"
    assert expense.amount == 45.99
    assert expense.tax == 3.50
    assert expense.kind == ExpenseKind.MATERIAL_PURCHASE
    assert expense.job_id == "job-001"
    assert expense.image_bytes == b"fake receipt image"

    # Verify: item marked processed (not fetched again)
    assert "test-001" in inbox.processed_ids
    items = await inbox.fetch_items()
    assert len(items) == 0

    # Verify: no items sent to review (success path)
    assert len(review_queue.items) == 0


async def test_orchestrator_handles_multiple_items():
    """Process multiple inbox items through the pipeline."""
    inbox = StubInboxSource(
        items=[
            InboxItem(
                inbox_id="item-1",
                subject="Supplies",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc),
                image_bytes=b"img1",
            ),
            InboxItem(
                inbox_id="item-2",
                subject="Catering",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 11, 0, 0, tzinfo=timezone.utc),
                image_bytes=b"img2",
            ),
        ]
    )
    extractor = StubExtractor()
    resolver = StubJobResolver()
    sink = StubExpenseSink()
    review_queue = StubReviewQueue()

    orchestrator = Orchestrator(inbox, extractor, resolver, sink, review_queue)
    await orchestrator.run()

    assert len(sink.stored) == 2
    assert sink.stored[0].merchant == "Office Depot"
    assert sink.stored[1].merchant == "Office Depot"
    assert len(review_queue.items) == 0


async def test_orchestrator_routes_unmatched_to_review():
    """Unmatched jobs route to review and mark processed (no re-loop)."""
    inbox = StubInboxSource(
        items=[
            InboxItem(
                inbox_id="unmatched-001",
                subject="Unknown Project",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc),
                image_bytes=b"fake receipt image",
            )
        ]
    )
    extractor = StubExtractor()

    # Resolver that returns None (no match)
    resolver = AsyncMock()
    resolver.resolve.return_value = None

    sink = StubExpenseSink()
    review_queue = StubReviewQueue()

    orchestrator = Orchestrator(inbox, extractor, resolver, sink, review_queue)
    await orchestrator.run()

    # Verify: not stored
    assert len(sink.stored) == 0

    # Verify: submitted to review with reason
    assert len(review_queue.items) == 1
    item, reason, partial = review_queue.items[0]
    assert item.subject == "Unknown Project"
    assert "No job match" in reason
    assert partial is not None  # Has partial extraction

    # Verify: marked processed (won't re-fetch)
    assert "unmatched-001" in inbox.processed_ids


async def test_orchestrator_routes_extraction_failure_to_review():
    """Extraction failure routes to review and marks processed (no re-loop)."""
    inbox = StubInboxSource(
        items=[
            InboxItem(
                inbox_id="bad-image-001",
                subject="Unreadable Receipt",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 10, 0, 0, tzinfo=timezone.utc),
                image_bytes=b"fake heic image",
            )
        ]
    )

    # Extractor that raises ValueError
    extractor = AsyncMock()
    extractor.extract.side_effect = ValueError("HEIC format not supported")

    resolver = StubJobResolver()
    sink = StubExpenseSink()
    review_queue = StubReviewQueue()

    orchestrator = Orchestrator(inbox, extractor, resolver, sink, review_queue)
    await orchestrator.run()

    # Verify: not stored
    assert len(sink.stored) == 0

    # Verify: submitted to review with reason
    assert len(review_queue.items) == 1
    item, reason, partial = review_queue.items[0]
    assert item.subject == "Unreadable Receipt"
    assert "Processing failed" in reason
    assert "HEIC format not supported" in reason
    assert partial is None  # Failed during extraction

    # Verify: marked processed (won't re-fetch)
    assert "bad-image-001" in inbox.processed_ids
