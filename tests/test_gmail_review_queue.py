"""Tests for Gmail review queue — mocked Gmail API, hermetic."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.gmail_review_queue import GmailReviewQueue
from core.ports import ExtractedExpense, ExpenseKind, InboxItem


@pytest.fixture
def review_queue():
    """Create a GmailReviewQueue with test config."""
    return GmailReviewQueue(
        token_path="./secrets/token.json",
        review_label="needs-review",
        notify_email="notify@example.com",
    )


@pytest.fixture
def sample_item():
    """Sample inbox item awaiting review."""
    return InboxItem(
        inbox_id="msg-001_0",
        subject="Unknown Project - May 15",
        sender="alice@example.com",
        received_at=datetime(2026, 5, 15, 10, 30, 0, tzinfo=timezone.utc),
        image_bytes=b"fake receipt image",
    )


@pytest.fixture
def partial_extraction():
    """Partial extraction data (before failure)."""
    return ExtractedExpense(
        merchant="Office Depot",
        amount=45.99,
        tax=3.50,
        date=datetime(2026, 5, 15, tzinfo=timezone.utc),
        kind=ExpenseKind.MATERIAL_PURCHASE,
        description="Office supplies",
    )


async def test_submit_applies_review_label(review_queue, sample_item):
    """Submitting an item applies the review label."""
    with patch.object(review_queue, "_authenticate", new_callable=AsyncMock):
        with patch.object(review_queue, "_log_review_details"):
            review_queue.service = MagicMock()
            review_queue.service.users().labels().list().execute.return_value = {
                "labels": [{"name": "needs-review", "id": "label-review-123"}]
            }

            await review_queue.submit(
                sample_item,
                "No job match for 'Unknown Project'",
            )

            # Verify modify was called to add the label
            modify_call = review_queue.service.users().messages().modify
            modify_call.assert_called_once()
            call_kwargs = modify_call.call_args[1]
            assert call_kwargs["id"] == "msg-001"
            assert "label-review-123" in call_kwargs["body"]["addLabelIds"]


async def test_submit_logs_details_with_partial(review_queue, sample_item, partial_extraction):
    """Submitting an item logs review details with partial extraction."""
    with patch.object(review_queue, "_authenticate", new_callable=AsyncMock):
        with patch.object(review_queue, "_log_review_details") as mock_log:
            with patch.object(review_queue, "_get_label_id", return_value="label-review-123"):
                review_queue.service = MagicMock()
                mock_cm = MagicMock()
                review_queue.service.users().messages().modify.return_value = mock_cm

                reason = "No job match for 'Unknown Project'"
                await review_queue.submit(sample_item, reason, partial=partial_extraction)

                # Verify logging was called with correct args
                mock_log.assert_called_once()
                call_args = mock_log.call_args[0]
                assert call_args[0] == sample_item
                assert call_args[1] == reason
                assert call_args[2] == partial_extraction


async def test_submit_logs_details_without_partial(review_queue, sample_item):
    """Submitting without partial extraction logs reason only."""
    with patch.object(review_queue, "_authenticate", new_callable=AsyncMock):
        with patch.object(review_queue, "_log_review_details") as mock_log:
            with patch.object(review_queue, "_get_label_id", return_value="label-review-123"):
                review_queue.service = MagicMock()
                mock_cm = MagicMock()
                review_queue.service.users().messages().modify.return_value = mock_cm

                reason = "HEIC format not supported"
                await review_queue.submit(sample_item, reason, partial=None)

                # Verify logging was called without partial
                mock_log.assert_called_once()
                call_args = mock_log.call_args[0]
                assert call_args[2] is None


async def test_label_created_if_not_exists(review_queue, sample_item):
    """Creates the review label if it doesn't exist."""
    with patch.object(review_queue, "_authenticate", new_callable=AsyncMock):
        with patch.object(review_queue, "_log_review_details"):
            review_queue.service = MagicMock()

            # No label exists initially
            review_queue.service.users().labels().list().execute.return_value = {
                "labels": []
            }
            # Create returns the new label
            review_queue.service.users().labels().create().execute.return_value = {
                "id": "label-new-123"
            }
            # Mock the modify call
            review_queue.service.users().messages().modify().execute.return_value = {}

            await review_queue.submit(sample_item, "Test reason")

            # Verify create was called (check the actual call with arguments)
            create_calls = [
                c for c in review_queue.service.users().labels().create.call_args_list
                if c[1].get("body")
            ]
            assert len(create_calls) >= 1
            call_kwargs = create_calls[0][1]
            assert call_kwargs["body"]["name"] == "needs-review"
