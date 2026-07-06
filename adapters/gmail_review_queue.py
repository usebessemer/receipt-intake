"""Gmail review queue — apply label and log review details."""

import logging
from datetime import datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.ports import ExtractedExpense, InboxItem, ReviewQueue

logger = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailReviewQueue(ReviewQueue):
    """Submit items to review: apply label and log details."""

    def __init__(
        self,
        token_path: str,
        review_label: str,
        notify_email: str,
    ):
        """Initialize Gmail review queue.

        Args:
            token_path: Path to OAuth token.
            review_label: Gmail label to apply (e.g., "needs-review").
            notify_email: Email address for future notifications (unused in v1).
        """
        self.token_path = token_path
        self.review_label = review_label
        self.notify_email = notify_email
        self.service = None

    async def _authenticate(self) -> None:
        """Authenticate to Gmail API."""
        if self.service is not None:
            return

        creds = Credentials.from_authorized_user_file(self.token_path, GMAIL_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        self.service = build("gmail", "v1", credentials=creds)

    async def submit(
        self,
        item: InboxItem,
        reason: str,
        partial: ExtractedExpense | None = None,
    ) -> None:
        """Submit item to review by applying a Gmail label.

        v1 surfaces review items via the `needs-review` label (visible in inbox).
        Active notification (email/SMS) is a follow-up issue.

        Args:
            item: The inbox item awaiting review.
            reason: Why it needs review (e.g., "no job match" or "extraction failed").
            partial: Optional partial extraction data.
        """
        await self._authenticate()

        # Extract message ID from inbox_id (format: "msg_id_attachment_index")
        msg_id = item.inbox_id.split("_")[0]

        try:
            # Apply review label to the message (v1 discovery mechanism)
            label_id = self._get_label_id(self.review_label)
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [label_id]},
            ).execute()
            logger.debug(f"Applied review label to message {msg_id}")

            # Log the review details for context
            self._log_review_details(item, reason, partial)

            logger.info(
                f"Submitted to review: {item.subject} ({reason})"
            )
        except HttpError as e:
            logger.error(f"Failed to submit to review: {e}")
            raise ValueError(f"Review submission failed: {e}") from e

    def _get_label_id(self, label_name: str) -> str:
        """Get or create a Gmail label by name."""
        labels = self.service.users().labels().list(userId="me").execute()
        for label in labels.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        # Create label if it doesn't exist
        label_body = {
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        }
        created = (
            self.service.users()
            .labels()
            .create(userId="me", body=label_body)
            .execute()
        )
        return created["id"]

    def _log_review_details(
        self,
        item: InboxItem,
        reason: str,
        partial: ExtractedExpense | None = None,
    ) -> None:
        """Log review item details for context.

        v1: items are surfaced via the 'needs-review' label in Gmail.
        Active notification (email/SMS/webhook) is a follow-up issue.
        """
        lines = [
            f"Subject: {item.subject}",
            f"From: {item.sender}",
            f"Received: {item.received_at.isoformat()}",
            f"Reason: {reason}",
        ]

        if partial:
            lines.extend([
                "",
                "Partial extraction:",
                f"  Merchant: {partial.merchant}",
                f"  Amount: ${partial.amount} + ${partial.tax} tax",
                f"  Kind: {partial.kind.value}",
            ])

        message_body = "\n".join(lines)
        logger.info(f"Review item: {message_body}")
