"""Port interfaces — abstract contracts that adapters implement.

Adapters (Gmail inbox, Claude extraction, a target-system DB, etc.) plug into these
ports. The core orchestrator knows only about these interfaces; it never
depends on a concrete adapter.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ExpenseKind(str, Enum):
    """Classification of expense type for routing to the correct sink table."""
    MATERIAL_PURCHASE = "material_purchase"
    TRANSPORT = "transport"
    CATERING = "catering"
    EQUIPMENT = "equipment"
    RENTAL = "rental"
    OTHER = "other"


@dataclass(frozen=True)
class InboxItem:
    """A receipt ready for extraction: email + attachment."""
    inbox_id: str
    subject: str
    sender: str
    received_at: datetime
    image_bytes: bytes


@dataclass(frozen=True)
class ExtractedExpense:
    """Structured expense extracted from a receipt image."""
    merchant: str
    amount: float
    tax: float
    date: datetime
    kind: ExpenseKind
    description: str


@dataclass(frozen=True)
class Expense:
    """An expense linked to a job and ready to store."""
    job_id: str
    merchant: str
    amount: float
    tax: float
    date: datetime
    kind: ExpenseKind
    description: str
    image_bytes: bytes


class InboxSource(ABC):
    """Fetches inbox items awaiting extraction."""

    @abstractmethod
    async def fetch_items(self) -> list[InboxItem]:
        """Return all inbox items awaiting processing."""
        pass

    @abstractmethod
    async def mark_processed(self, inbox_id: str) -> None:
        """Mark an item as processed (remove from inbox)."""
        pass


class Extractor(ABC):
    """Extracts expense data from receipt images."""

    @abstractmethod
    async def extract(self, image_bytes: bytes, subject: str) -> ExtractedExpense:
        """Extract expense fields from an image."""
        pass


class JobResolver(ABC):
    """Resolves a receipt to an existing job."""

    @abstractmethod
    async def resolve(self, expense: ExtractedExpense, subject: str) -> str | None:
        """Match receipt to a job using the subject hint.

        Args:
            expense: Extracted expense data (has merchant, amount, etc.).
            subject: Job-name hint from email subject (generic string, not system-specific).

        Returns:
            job_id (str) if matched with sufficient confidence, else None (routes to review).
        """
        pass


class ExpenseSink(ABC):
    """Stores extracted expenses."""

    @abstractmethod
    async def store(self, expense: Expense) -> None:
        """Persist an expense."""
        pass


class ReviewQueue(ABC):
    """Routes unmatched or uncertain items for human review."""

    @abstractmethod
    async def submit(
        self,
        item: InboxItem,
        reason: str,
        partial: ExtractedExpense | None = None,
    ) -> None:
        """Submit an item to the review queue.

        Args:
            item: The inbox item (email context: subject, sender, image).
            reason: Why it needs review (e.g., "no job match for 'X'" or "HEIC format not supported").
            partial: Optional partial extraction (e.g., if extraction failed mid-stream).
        """
        pass
