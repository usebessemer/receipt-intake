"""Gmail inbox adapter (InboxSource) — OAuth + unread message polling."""

import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from core.ports import InboxItem, InboxSource

logger = logging.getLogger(__name__)

# Gmail API scopes required (must match the token for refresh to work)
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


class GmailInbox(InboxSource):
    """Poll Gmail for unread messages with image attachments."""

    def __init__(
        self,
        intake_address: str,
        oauth_client_path: str,
        token_path: str,
        processed_label: str,
        query: str = "is:unread has:attachment",
    ):
        """Initialize Gmail inbox adapter.

        Args:
            intake_address: Gmail address to monitor (e.g., receipts.example@gmail.com)
            oauth_client_path: Path to OAuth client credentials JSON
            token_path: Path to store/load OAuth token
            processed_label: Label to apply when processed (e.g., "processed")
            query: Gmail search query (default: unread with attachments)
        """
        self.intake_address = intake_address
        self.oauth_client_path = oauth_client_path
        self.token_path = token_path
        self.processed_label = processed_label
        self.query = query
        self.service = None

    async def _authenticate(self) -> None:
        """Authenticate to Gmail API using OAuth token."""
        if self.service is not None:
            return

        creds = None

        # Load existing token if present
        token_file = Path(self.token_path)
        if token_file.exists():
            creds = Credentials.from_authorized_user_file(
                str(token_file), GMAIL_SCOPES
            )
            logger.debug(f"Loaded OAuth token from {self.token_path}")

        # Refresh if expired or missing
        if creds is None or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logger.debug("Refreshing expired OAuth token")
                creds.refresh(Request())
            else:
                # Fallback: start installed app flow (will prompt for browser consent)
                logger.info(
                    f"No valid token found. Starting OAuth flow for {self.intake_address}"
                )
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.oauth_client_path, GMAIL_SCOPES
                )
                creds = flow.run_local_server(port=0)

            # Save token for next run
            token_file.parent.mkdir(parents=True, exist_ok=True)
            with open(str(token_file), "w") as f:
                f.write(creds.to_json())
            logger.debug(f"Saved OAuth token to {self.token_path}")

        self.service = build("gmail", "v1", credentials=creds)

    async def fetch_items(self) -> list[InboxItem]:
        """Fetch unread messages with image attachments.

        Returns:
            List of InboxItems (one per image attachment).
        """
        await self._authenticate()

        items = []
        try:
            # Search for unread messages with attachments
            results = (
                self.service.users()
                .messages()
                .list(userId="me", q=self.query, maxResults=10)
                .execute()
            )
            message_ids = [m["id"] for m in results.get("messages", [])]

            for msg_id in message_ids:
                msg = (
                    self.service.users()
                    .messages()
                    .get(userId="me", id=msg_id, format="full")
                    .execute()
                )

                # Extract headers
                headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
                subject = headers.get("Subject", "(no subject)")
                sender = headers.get("From", "(no sender)")
                received_at_str = headers.get("Date", "")

                # Parse received_at (Gmail uses RFC 2822)
                try:
                    from email.utils import parsedate_to_datetime
                    received_at = parsedate_to_datetime(received_at_str)
                except (TypeError, ValueError):
                    received_at = datetime.now(timezone.utc)
                    logger.warning(
                        f"Failed to parse date {received_at_str}, using now()"
                    )

                # Extract image attachments
                image_items = await self._extract_images(msg_id, msg, subject, sender, received_at)
                items.extend(image_items)

        except HttpError as e:
            logger.error(f"Failed to fetch Gmail messages: {e}")
            raise ValueError(f"Gmail API error: {e}") from e

        return items

    async def _extract_images(
        self, msg_id: str, msg: dict, subject: str, sender: str, received_at: datetime
    ) -> list[InboxItem]:
        """Extract image attachments from a message.

        Recursively searches MIME parts (handles forwarded emails,
        multipart/related, etc.). Yields one InboxItem per image attachment.
        """
        items = []
        attachment_index = 0

        def collect_images(part: dict) -> None:
            """Recursively collect image attachments from a part and its children."""
            nonlocal attachment_index

            mime_type = part.get("mimeType", "")

            # Base case: this part is an image with an attachment ID
            if mime_type.startswith("image/"):
                attachment_id = part.get("body", {}).get("attachmentId")
                if attachment_id:
                    try:
                        attachment = (
                            self.service.users()
                            .messages()
                            .attachments()
                            .get(userId="me", messageId=msg_id, id=attachment_id)
                            .execute()
                        )
                        image_data = attachment.get("data", "")
                        image_bytes = base64.urlsafe_b64decode(image_data)

                        item = InboxItem(
                            inbox_id=f"{msg_id}_{attachment_index}",
                            subject=subject,
                            sender=sender,
                            received_at=received_at,
                            image_bytes=image_bytes,
                        )
                        items.append(item)
                        attachment_index += 1
                        logger.debug(f"Extracted image from {subject}")
                    except Exception as e:
                        logger.error(f"Failed to extract attachment {attachment_id}: {e}")

            # Recurse into nested parts (forwarded, multipart/related, etc.)
            for child_part in part.get("parts", []):
                collect_images(child_part)

        payload = msg.get("payload", {})
        collect_images(payload)

        return items

    async def mark_processed(self, inbox_id: str) -> None:
        """Mark a message as processed (remove unread, apply label).

        Args:
            inbox_id: Format is "{message_id}_{attachment_index}"
        """
        await self._authenticate()

        msg_id = inbox_id.split("_")[0]

        try:
            # Remove UNREAD label
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"removeLabelIds": ["UNREAD"]},
            ).execute()

            # Apply processed label
            self.service.users().messages().modify(
                userId="me",
                id=msg_id,
                body={"addLabelIds": [self._get_label_id(self.processed_label)]},
            ).execute()

            logger.debug(f"Marked message {msg_id} as processed")
        except HttpError as e:
            logger.error(f"Failed to mark message {msg_id} as processed: {e}")
            raise ValueError(f"Failed to mark processed: {e}") from e

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
