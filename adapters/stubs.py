"""Stub adapters for testing — no external dependencies.

Used in the walking skeleton to prove the end-to-end pipeline works before
real adapters (Gmail, Claude, a target-system sink) exist.
"""

import logging
from datetime import datetime, timezone

from core.ports import (
    Expense,
    ExpenseKind,
    ExpenseSink,
    Extractor,
    ExtractedExpense,
    InboxItem,
    InboxSource,
    JobResolver,
    ReviewQueue,
)

logger = logging.getLogger(__name__)


class StubInboxSource(InboxSource):
    """Fake inbox: yields a hardcoded test receipt."""

    def __init__(self, items: list[InboxItem] | None = None):
        self.items = items or [
            InboxItem(
                inbox_id="stub-001",
                subject="Office supplies, May 15",
                sender="user@example.com",
                received_at=datetime(2026, 5, 15, 10, 30, 0, tzinfo=timezone.utc),
                image_bytes=b"fake receipt image data",
            )
        ]
        self.processed_ids = set()

    async def fetch_items(self) -> list[InboxItem]:
        return [item for item in self.items if item.inbox_id not in self.processed_ids]

    async def mark_processed(self, inbox_id: str) -> None:
        self.processed_ids.add(inbox_id)


class StubExtractor(Extractor):
    """Fake extraction: echoes back structured data based on subject."""

    async def extract(self, image_bytes: bytes, subject: str) -> ExtractedExpense:
        # Naive stub: parse the subject for keywords
        return ExtractedExpense(
            merchant="Office Depot",
            amount=45.99,
            tax=3.50,
            date=datetime(2026, 5, 15, tzinfo=timezone.utc),
            kind=ExpenseKind.MATERIAL_PURCHASE,
            description=f"Receipt: {subject}",
        )


class StubJobResolver(JobResolver):
    """Fake job resolution: resolves to a hardcoded job."""

    async def resolve(self, expense: ExtractedExpense, subject: str) -> str | None:
        # Stub always finds the job. Return "job-001" for testing.
        return "job-001"


class StubExpenseSink(ExpenseSink):
    """Fake storage: prints to console."""

    def __init__(self):
        self.stored = []

    async def store(self, expense: Expense) -> None:
        self.stored.append(expense)
        logger.info(
            f"[STUB STORE] {expense.merchant} ${expense.amount} → {expense.job_id}"
        )


class StubReviewQueue(ReviewQueue):
    """Fake review queue: logs to console."""

    def __init__(self):
        self.items = []

    async def submit(
        self,
        item: InboxItem,
        reason: str,
        partial: ExtractedExpense | None = None,
    ) -> None:
        self.items.append((item, reason, partial))
        logger.info(f"[STUB REVIEW] {item.subject}: {reason}")
