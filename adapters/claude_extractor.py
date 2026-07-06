"""Claude vision receipt extractor — image → structured expense via tool use."""

import base64
import logging
from datetime import datetime, timezone

from anthropic import AsyncAnthropic

from core.ports import Extractor, ExtractedExpense, ExpenseKind

logger = logging.getLogger(__name__)

EXTRACTION_SYSTEM_PROMPT = """You are a receipt extraction specialist. Extract structured data from receipt images.
Return the following fields:
- merchant: vendor/business name
- amount: subtotal (pre-tax) as a float. POSITIVE for a normal purchase; NEGATIVE for a refund, return, or credit.
- tax: HST/sales tax amount as a float. Its sign MUST match the amount — positive on a purchase, negative on a refund/return/credit (a refund reduces the reclaimable tax too).
- date: receipt date in ISO format (YYYY-MM-DD)
- kind: expense classification
- description: short description of the purchase

REFUNDS / RETURNS / CREDITS: when the receipt is a refund, return, or credit, sign BOTH the amount and the tax NEGATIVE. Cues to look for:
- the words "REFUND", "RETURN", "CREDIT", or "VOID"
- a negative total, or amounts shown in parentheses — e.g. ($22.19) means -22.19
- money credited back to a card
A normal purchase keeps positive amounts.

For 'kind', choose from:
- material_purchase: supplies, materials, inventory
- transport: gas, parking, vehicle maintenance
- catering: food, beverages, meals
- equipment: tools, hardware, machinery
- rental: space, equipment rental
- other: miscellaneous
Classify a refund by what was bought — a returned tool is still 'equipment', just negative.

If a field is unreadable or missing, use null."""

EXTRACT_TOOL = {
    "name": "extract_receipt",
    "description": "Extract structured expense data from a receipt image",
    "input_schema": {
        "type": "object",
        "properties": {
            "merchant": {
                "type": ["string", "null"],
                "description": "Vendor/business name",
            },
            "amount": {
                "type": ["number", "null"],
                "description": "Subtotal amount (pre-tax). Positive for a purchase; NEGATIVE for a refund, return, or credit.",
            },
            "tax": {
                "type": ["number", "null"],
                "description": "Tax/HST amount. Sign matches the amount — negative for a refund, return, or credit.",
            },
            "date": {
                "type": ["string", "null"],
                "description": "Receipt date in ISO format (YYYY-MM-DD)",
            },
            "kind": {
                "type": ["string", "null"],
                "enum": [
                    "material_purchase",
                    "transport",
                    "catering",
                    "equipment",
                    "rental",
                    "other",
                    None,
                ],
                "description": "Expense classification",
            },
            "description": {
                "type": ["string", "null"],
                "description": "Short description of the purchase",
            },
        },
        "required": [
            "merchant",
            "amount",
            "tax",
            "date",
            "kind",
            "description",
        ],
    },
}


def _detect_image_format(image_bytes: bytes) -> str:
    """Detect image format from magic bytes; raise if unsupported."""
    if len(image_bytes) < 12:
        raise ValueError("Image data too small to determine format")

    # Magic numbers (hex) for supported formats
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    elif image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    elif image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    elif image_bytes[:6] == b"GIF87a" or image_bytes[:6] == b"GIF89a":
        return "image/gif"
    # ISO-BMFF (HEIC/HEIF): 'ftyp' box at offset 4, brand at offset 8
    elif image_bytes[4:8] == b"ftyp" and image_bytes[8:12] in (
        b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1",
    ):
        raise ValueError(
            "HEIC/HEIF images not supported by Claude vision. "
            "Convert to JPEG/PNG first or use a different receipt format."
        )
    else:
        raise ValueError(
            f"Unsupported image format. Supported: JPEG, PNG, WebP, GIF. "
            f"Got magic bytes: {image_bytes[:8].hex()}"
        )


class ClaudeExtractor(Extractor):
    """Extract expense data from receipt images using Claude vision."""

    def __init__(self, api_key: str | None = None):
        self.client = AsyncAnthropic(api_key=api_key)
        self.model = "claude-haiku-4-5-20251001"

    async def extract(self, image_bytes: bytes, subject: str) -> ExtractedExpense:
        """Extract structured expense from a receipt image.

        Args:
            image_bytes: Receipt image data (JPEG, PNG, WebP, or GIF).
            subject: Email subject line for context.

        Returns:
            ExtractedExpense with extracted fields; unreadable fields are None.

        Raises:
            ValueError: If image format unsupported or extraction fails.
        """
        # Detect image format from magic bytes; raises ValueError if unsupported
        media_type = _detect_image_format(image_bytes)
        image_base64 = base64.standard_b64encode(image_bytes).decode("utf-8")

        # Call Claude with prompt caching to extract receipt data
        response = await self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": EXTRACTION_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": "extract_receipt"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_base64,
                            },
                        },
                        {
                            "type": "text",
                            "text": f"Extract data from this receipt. Subject: {subject}",
                        },
                    ],
                }
            ],
        )

        # Parse tool use result
        tool_call = None
        for content_block in response.content:
            if content_block.type == "tool_use":
                tool_call = content_block
                break

        if not tool_call:
            raise ValueError("Claude did not return a tool use result")

        # Validate and construct ExtractedExpense
        extracted_data = tool_call.input
        return self._build_expense(extracted_data)

    def _build_expense(self, data: dict) -> ExtractedExpense:
        """Convert extracted data to ExtractedExpense, handling None values."""
        merchant = data.get("merchant")
        if not merchant:
            raise ValueError("Merchant is required but missing or unreadable")

        amount = data.get("amount")
        if amount is None:
            raise ValueError("Amount is required but missing or unreadable")
        amount = float(amount)

        tax = data.get("tax")
        if tax is None:
            tax = 0.0
        tax = float(tax)

        # Refunds/returns/credits are signed negative (see docs/DECISIONS.md).
        # The tax sign must follow the amount sign — a refund reduces the HST
        # reclaim too. Enforce it here so a model slip on one sign can't desync
        # amount and tax (the exact 2026-06-05 failure: a refund's amount came
        # back negative but tax positive).
        if amount < 0:
            tax = -abs(tax)
        elif amount > 0:
            tax = abs(tax)

        date_str = data.get("date")
        if not date_str:
            raise ValueError("Date is required but missing or unreadable")
        try:
            date = datetime.fromisoformat(date_str)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid date format: {date_str}") from e

        kind_str = data.get("kind")
        if not kind_str:
            raise ValueError("Kind is required but missing or unreadable")
        try:
            kind = ExpenseKind(kind_str)
        except ValueError as e:
            raise ValueError(f"Invalid kind: {kind_str}") from e

        description = data.get("description") or ""

        return ExtractedExpense(
            merchant=merchant,
            amount=amount,
            tax=tax,
            date=date,
            kind=kind,
            description=description,
        )
