"""Tests for Claude vision receipt extractor — mocked Anthropic responses."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.claude_extractor import ClaudeExtractor, _detect_image_format
from core.ports import ExpenseKind


@pytest.fixture
def extractor():
    """Create a ClaudeExtractor instance."""
    return ClaudeExtractor(api_key="test-key")


def _mock_response(merchant="Office Depot", amount=45.99, tax=3.50,
                   date="2026-05-15", kind="material_purchase",
                   description="Office supplies"):
    """Build a mock Anthropic response."""
    tool_use = MagicMock()
    tool_use.type = "tool_use"
    tool_use.name = "extract_receipt"
    tool_use.input = {
        "merchant": merchant,
        "amount": amount,
        "tax": tax,
        "date": date,
        "kind": kind,
        "description": description,
    }

    response = MagicMock()
    response.content = [tool_use]
    return response


async def test_extract_valid_receipt(extractor):
    """Extract a well-formed receipt."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response()

        # JPEG magic bytes
        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        result = await extractor.extract(
            image_bytes=jpeg_data,
            subject="Office supplies receipt",
        )

        assert result.merchant == "Office Depot"
        assert result.amount == 45.99
        assert result.tax == 3.50
        assert result.date == datetime(2026, 5, 15, tzinfo=timezone.utc)
        assert result.kind == ExpenseKind.MATERIAL_PURCHASE
        assert result.description == "Office supplies"


async def test_extract_normal_purchase_is_positive(extractor):
    """A normal purchase keeps positive amount and tax."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(amount=84.99, tax=11.05)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        result = await extractor.extract(jpeg_data, "Hardware Depot supplies")

        assert result.amount == 84.99
        assert result.tax == 11.05


async def test_extract_refund_is_negative(extractor):
    """A refund/return/credit yields a negative amount AND negative tax."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        # The 2026-05-20 Hardware Depot refund: should be -22.19, not +22.19
        mock_create.return_value = _mock_response(amount=-22.19, tax=-2.88)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake refund receipt"
        result = await extractor.extract(jpeg_data, "Hardware Depot REFUND")

        assert result.amount == -22.19
        assert result.tax == -2.88


async def test_extract_refund_tax_sign_follows_negative_amount(extractor):
    """If the model signs amount negative but tax positive, tax is corrected negative."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        # Model slip: amount negative (refund) but tax came back positive
        mock_create.return_value = _mock_response(amount=-22.19, tax=2.88)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake refund receipt"
        result = await extractor.extract(jpeg_data, "Hardware Depot RETURN")

        assert result.amount == -22.19
        assert result.tax == -2.88  # corrected to match amount sign


async def test_extract_purchase_tax_sign_follows_positive_amount(extractor):
    """If the model signs a purchase's tax negative, tax is corrected positive."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        # Model slip: positive purchase but tax came back negative
        mock_create.return_value = _mock_response(amount=50.0, tax=-6.50)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        result = await extractor.extract(jpeg_data, "supplies")

        assert result.amount == 50.0
        assert result.tax == 6.50  # corrected to match amount sign


async def test_extract_zero_tax(extractor):
    """Handle zero tax (e.g., food receipts in some jurisdictions)."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(tax=0.0)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        result = await extractor.extract(jpeg_data, "food receipt")

        assert result.tax == 0.0


async def test_extract_all_expense_kinds(extractor):
    """Verify all valid expense kinds are accepted."""
    kinds_and_enums = [
        ("material_purchase", ExpenseKind.MATERIAL_PURCHASE),
        ("transport", ExpenseKind.TRANSPORT),
        ("catering", ExpenseKind.CATERING),
        ("equipment", ExpenseKind.EQUIPMENT),
        ("rental", ExpenseKind.RENTAL),
        ("other", ExpenseKind.OTHER),
    ]

    for kind_str, kind_enum in kinds_and_enums:
        with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = _mock_response(kind=kind_str)

            jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
            result = await extractor.extract(jpeg_data, "test")

            assert result.kind == kind_enum


async def test_extract_missing_merchant_raises(extractor):
    """Missing merchant is a hard error."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(merchant=None)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Merchant.*required"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_missing_amount_raises(extractor):
    """Missing amount is a hard error."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(amount=None)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Amount.*required"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_missing_date_raises(extractor):
    """Missing date is a hard error."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(date=None)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Date.*required"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_invalid_date_format_raises(extractor):
    """Invalid date format raises ValueError."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(date="not-a-date")

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Invalid date format"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_missing_kind_raises(extractor):
    """Missing kind is a hard error."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(kind=None)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Kind.*required"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_invalid_kind_raises(extractor):
    """Invalid kind value raises ValueError."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(kind="invalid_kind")

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="Invalid kind"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_missing_tool_use_raises(extractor):
    """If Claude doesn't return a tool use, raise ValueError."""
    response = MagicMock()
    response.content = [MagicMock(type="text")]  # Not a tool_use

    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = response

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        with pytest.raises(ValueError, match="tool use"):
            await extractor.extract(jpeg_data, "test")


async def test_extract_empty_description_defaults_to_empty_string(extractor):
    """Empty or None description becomes empty string."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response(description=None)

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        result = await extractor.extract(jpeg_data, "test")

        assert result.description == ""


async def test_extract_uses_prompt_caching(extractor):
    """Verify system prompt includes cache_control."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response()

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        await extractor.extract(jpeg_data, "test")

        # Check that cache_control was passed in system prompt
        call_args = mock_create.call_args
        system_prompt = call_args.kwargs["system"]
        assert isinstance(system_prompt, list)
        assert system_prompt[0].get("cache_control") == {"type": "ephemeral"}


async def test_extract_sends_image_as_base64(extractor):
    """Verify image is base64-encoded in the request."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response()

        jpeg_data = b"\xff\xd8\xff\xe0" + b"test image bytes"
        await extractor.extract(jpeg_data, "test")

        call_args = mock_create.call_args
        messages = call_args.kwargs["messages"]
        image_content = [
            c for c in messages[0]["content"] if c.get("type") == "image"
        ][0]

        # Image should be base64-encoded
        import base64

        assert (
            base64.standard_b64decode(
                image_content["source"]["data"]
            )
            == jpeg_data
        )


async def test_extract_uses_haiku_model(extractor):
    """Verify the correct Claude model is used."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response()

        jpeg_data = b"\xff\xd8\xff\xe0" + b"fake receipt"
        await extractor.extract(jpeg_data, "test")

        call_args = mock_create.call_args
        assert call_args.kwargs["model"] == "claude-haiku-4-5-20251001"


def test_detect_image_format_jpeg():
    """Detect JPEG format from magic bytes."""
    jpeg_data = b"\xff\xd8\xff\xe0" + b"jpeg content"
    assert _detect_image_format(jpeg_data) == "image/jpeg"


def test_detect_image_format_png():
    """Detect PNG format from magic bytes."""
    png_data = b"\x89PNG\r\n\x1a\n" + b"png content"
    assert _detect_image_format(png_data) == "image/png"


def test_detect_image_format_webp():
    """Detect WebP format from magic bytes."""
    webp_data = b"RIFF" + b"xxxx" + b"WEBP" + b"content"
    assert _detect_image_format(webp_data) == "image/webp"


def test_detect_image_format_gif():
    """Detect GIF87a and GIF89a formats."""
    gif87_data = b"GIF87a" + b"content"
    assert _detect_image_format(gif87_data) == "image/gif"

    gif89_data = b"GIF89a" + b"content"
    assert _detect_image_format(gif89_data) == "image/gif"


def test_detect_image_format_heic_raises():
    """HEIC format raises ValueError (not supported by Claude)."""
    # Real HEIC header: box size (4 bytes) + 'ftyp' (offset 4) + 'heic' brand (offset 8)
    heic_data = b"\x00\x00\x00\x18ftyp" + b"heic" + b"content"
    with pytest.raises(ValueError, match="HEIC/HEIF.*not supported"):
        _detect_image_format(heic_data)


def test_detect_image_format_heic_variants():
    """Detect various HEIC/HEIF brands."""
    brands = [b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"]
    for brand in brands:
        heic_data = b"\x00\x00\x00\x18ftyp" + brand + b"content"
        with pytest.raises(ValueError, match="HEIC/HEIF.*not supported"):
            _detect_image_format(heic_data)


def test_detect_image_format_unknown_raises():
    """Unknown format raises ValueError."""
    unknown_data = b"\x00\x00\x00\x00" + b"content" * 10  # Ensure it's > 12 bytes
    with pytest.raises(ValueError, match="Unsupported image format"):
        _detect_image_format(unknown_data)


def test_detect_image_format_too_small_raises():
    """Image data too small raises ValueError."""
    with pytest.raises(ValueError, match="Image data too small"):
        _detect_image_format(b"xx")


async def test_extract_png_uses_correct_media_type(extractor):
    """Verify PNG image uses image/png media type."""
    with patch.object(extractor.client.messages, "create", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = _mock_response()

        png_data = b"\x89PNG\r\n\x1a\n" + b"png content"
        await extractor.extract(png_data, "test")

        call_args = mock_create.call_args
        messages = call_args.kwargs["messages"]
        image_content = [
            c for c in messages[0]["content"] if c.get("type") == "image"
        ][0]

        # PNG should have image/png media type
        assert image_content["source"]["media_type"] == "image/png"
