"""Tests for Gmail inbox adapter — mocked Gmail API, hermetic."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.gmail_inbox import GmailInbox
from core.ports import InboxItem


@pytest.fixture
def gmail_inbox():
    """Create a GmailInbox with test config."""
    return GmailInbox(
        intake_address="test@example.com",
        oauth_client_path="./secrets/oauth_client.json",
        token_path="./secrets/token.json",
        processed_label="processed",
        query="is:unread has:attachment",
    )


@pytest.fixture
def mock_service(gmail_inbox):
    """Mock the Gmail service."""
    gmail_inbox.service = MagicMock()
    return gmail_inbox.service


@pytest.fixture
def sample_message_with_image():
    """Sample Gmail message with image attachment."""
    return {
        "id": "msg-001",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Acme Office Fitout - May 15"},
                {"name": "From", "value": "alice@example.com"},
                {
                    "name": "Date",
                    "value": "Mon, 15 May 2026 10:30:00 +0000",
                },
            ],
            "parts": [
                {
                    "mimeType": "image/jpeg",
                    "body": {"attachmentId": "att-001"},
                },
            ],
        },
    }


async def test_fetch_items_unread_with_image(gmail_inbox, mock_service, sample_message_with_image):
    """Fetch unread messages with image attachments."""
    # Mock list call
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg-001"}]
    }

    # Mock get call
    mock_service.users().messages().get().execute.return_value = sample_message_with_image

    # Mock attachment download
    import base64
    image_data = base64.urlsafe_b64encode(b"fake jpeg data").decode()
    mock_service.users().messages().attachments().get().execute.return_value = {
        "data": image_data
    }

    # Patch authenticate to skip OAuth
    with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
        items = await gmail_inbox.fetch_items()

    assert len(items) == 1
    item = items[0]
    assert isinstance(item, InboxItem)
    assert item.inbox_id == "msg-001_0"
    assert item.subject == "Acme Office Fitout - May 15"
    assert item.sender == "alice@example.com"
    assert item.image_bytes == b"fake jpeg data"


async def test_fetch_items_filters_unread_has_attachment(gmail_inbox, mock_service):
    """Verify fetch uses correct Gmail query (unread + attachment)."""
    mock_service.users().messages().list().execute.return_value = {"messages": []}

    with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
        await gmail_inbox.fetch_items()

    # Verify list() was called with the right query
    list_call = mock_service.users().messages().list
    # Get the actual call with arguments (skip the setup calls)
    calls = [c for c in list_call.call_args_list if c[1].get("q")]
    assert len(calls) >= 1
    call_kwargs = calls[0][1]
    assert call_kwargs["q"] == "is:unread has:attachment"


async def test_fetch_items_multiple_attachments(gmail_inbox, mock_service):
    """Yield one InboxItem per image attachment."""
    message = {
        "id": "msg-002",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Test"},
                {"name": "From", "value": "bob@example.com"},
                {"name": "Date", "value": "Mon, 15 May 2026 10:30:00 +0000"},
            ],
            "parts": [
                {"mimeType": "image/jpeg", "body": {"attachmentId": "att-1"}},
                {"mimeType": "application/pdf", "body": {"attachmentId": "att-2"}},  # Not an image
                {"mimeType": "image/png", "body": {"attachmentId": "att-3"}},
            ],
        },
    }

    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg-002"}]
    }
    mock_service.users().messages().get().execute.return_value = message

    import base64
    jpeg_data = base64.urlsafe_b64encode(b"jpeg").decode()
    png_data = base64.urlsafe_b64encode(b"png").decode()

    # Mock attachment calls
    def get_attachment(userId=None, messageId=None, id=None):
        if id == "att-1":
            return {"data": jpeg_data}
        elif id == "att-3":
            return {"data": png_data}
        return {}

    mock_service.users().messages().attachments().get().execute.side_effect = (
        lambda: get_attachment(
            id=mock_service.users().messages().attachments().get.call_args[1]["id"]
        )
    )

    with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
        items = await gmail_inbox.fetch_items()

    # Only 2 items (JPEG + PNG, not PDF)
    assert len(items) == 2
    assert items[0].inbox_id == "msg-002_0"  # First image
    assert items[1].inbox_id == "msg-002_1"  # Second image


async def test_fetch_items_nested_parts(gmail_inbox, mock_service):
    """Handle nested MIME parts (forwarded emails, multipart/related)."""
    # Message with nested structure: forwarded email with image inside
    message = {
        "id": "msg-003",
        "payload": {
            "headers": [
                {"name": "Subject", "value": "Fwd: Receipt"},
                {"name": "From", "value": "charlie@example.com"},
                {"name": "Date", "value": "Mon, 15 May 2026 10:30:00 +0000"},
            ],
            "parts": [
                {
                    "mimeType": "multipart/mixed",
                    "parts": [
                        {"mimeType": "text/plain", "body": {"data": ""}},
                        {
                            "mimeType": "multipart/related",
                            "parts": [
                                {"mimeType": "image/jpeg", "body": {"attachmentId": "nested-att-1"}},
                            ],
                        },
                    ],
                },
            ],
        },
    }

    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg-003"}]
    }
    mock_service.users().messages().get().execute.return_value = message

    import base64
    image_data = base64.urlsafe_b64encode(b"nested image").decode()
    mock_service.users().messages().attachments().get().execute.return_value = {
        "data": image_data
    }

    with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
        items = await gmail_inbox.fetch_items()

    # Should find the nested image
    assert len(items) == 1
    assert items[0].inbox_id == "msg-003_0"
    assert items[0].image_bytes == b"nested image"


async def test_mark_processed_removes_unread_adds_label(gmail_inbox, mock_service):
    """Mark message as processed: remove UNREAD, add label."""
    with patch.object(gmail_inbox, "_get_label_id", return_value="label-processed"):
        with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
            await gmail_inbox.mark_processed("msg-001_0")

    # Verify modify was called to remove UNREAD
    modify_calls = mock_service.users().messages().modify.call_args_list
    assert len(modify_calls) == 2

    # Check removeLabelIds
    remove_call = modify_calls[0]
    assert remove_call[1]["id"] == "msg-001"
    assert "UNREAD" in remove_call[1]["body"]["removeLabelIds"]

    # Check addLabelIds
    add_call = modify_calls[1]
    assert add_call[1]["id"] == "msg-001"
    assert "label-processed" in add_call[1]["body"]["addLabelIds"]


async def test_parse_received_at_datetime(gmail_inbox, mock_service, sample_message_with_image):
    """Parse received_at as proper datetime with timezone."""
    mock_service.users().messages().list().execute.return_value = {
        "messages": [{"id": "msg-001"}]
    }
    mock_service.users().messages().get().execute.return_value = sample_message_with_image

    import base64
    image_data = base64.urlsafe_b64encode(b"fake").decode()
    mock_service.users().messages().attachments().get().execute.return_value = {
        "data": image_data
    }

    with patch.object(gmail_inbox, "_authenticate", new_callable=AsyncMock):
        items = await gmail_inbox.fetch_items()

    assert len(items) == 1
    received_at = items[0].received_at
    assert isinstance(received_at, datetime)
    assert received_at.tzinfo is not None
    assert received_at.year == 2026
    assert received_at.month == 5
    assert received_at.day == 15
