"""Document conversion via markitdown."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..codex_service.context import SUMMARY_CHAR_LIMIT


async def document_text(path: Path) -> str:
    """Convert a document to full markdown text."""
    return await asyncio.to_thread(_convert_text, path)


async def document_summary(path: Path) -> str:
    """Convert a document to markdown and return the first 500 chars as summary."""
    text = await document_text(path)
    return text[:SUMMARY_CHAR_LIMIT]


def _convert_text(path: Path) -> str:
    from markitdown import MarkItDown

    result = MarkItDown().convert(str(path))
    return (result.text_content or "").strip()
