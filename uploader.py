"""Telegram upload helpers and Instagram caption cleanup."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Awaitable, TypeVar

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import FSInputFile, InputMediaPhoto, InputMediaVideo

T = TypeVar("T")

# Remove @mentions/usernames from captions.
_USERNAME_RE = re.compile(r"(?<![\w])@[A-Za-z0-9._-]{1,64}")

# Remove full links such as https://..., http://..., www.... and ftp://...
_URL_RE = re.compile(r"(?i)(?:https?://|ftp://|www\.)[^\s<>]+")

# Remove bare domains such as example.com, example.in, t.me/abc.
_DOMAIN_RE = re.compile(
    r"(?i)(?<![@\w])"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z]{2,63}(?:/[^\s<>]*)?"
)

_TRAILING_PUNCT_RE = re.compile(r"[.,!?;:]+$")


# =============================================================================
# 🧹 Caption cleanup
# =============================================================================


def sanitize_caption(text: str) -> str:
    """Remove usernames and website links while keeping useful caption text."""

    if not text:
        return ""

    cleaned = _URL_RE.sub("", text)
    cleaned = _DOMAIN_RE.sub("", cleaned)
    cleaned = _USERNAME_RE.sub("", cleaned)

    clean_lines: list[str] = []
    for line in cleaned.splitlines():
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        line = _TRAILING_PUNCT_RE.sub("", line).strip()
        if line:
            clean_lines.append(line)

    return "\n".join(clean_lines).strip()


# =============================================================================
# 📦 Media helpers
# =============================================================================


def chunks(items: list[Path], size: int) -> Iterable[list[Path]]:
    """Yield media in Telegram's 10-item media-group chunks."""

    for start in range(0, len(items), size):
        yield items[start : start + size]


def make_media(path: Path) -> InputMediaPhoto | InputMediaVideo:
    """Convert a local image/video path to an aiogram media object."""

    extension = path.suffix.lower()

    if extension in {".jpg", ".jpeg", ".png", ".webp"}:
        return InputMediaPhoto(media=FSInputFile(path))

    if extension in {".mp4", ".mov"}:
        return InputMediaVideo(
            media=FSInputFile(path),
            supports_streaming=True,
        )

    raise ValueError(f"Unsupported media type: {path.suffix}")


# =============================================================================
# 🔁 Telegram retry helper
# =============================================================================


async def _send_with_retry(
    sender: Callable[[], Awaitable[T]],
    *,
    max_retries: int = 3,
) -> T:
    """Retry temporary Telegram failures without retrying permanent errors."""

    attempt = 0

    while True:
        try:
            return await sender()
        except TelegramRetryAfter as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(max(1, int(exc.retry_after)) + 1)
        except (TelegramNetworkError, TelegramServerError):
            attempt += 1
            if attempt > max_retries:
                raise
            await asyncio.sleep(min(30, 2**attempt))
        except (TelegramBadRequest, TelegramForbiddenError):
            raise


# =============================================================================
# 📤 Main upload function
# =============================================================================


async def send_post(
    bot: Bot,
    chat_id: int,
    files: list[Path],
    caption: str,
    *,
    source_kind: str = "post",
    upload_delay: float = 0.7,
    max_retries: int = 3,
) -> None:
    """Upload one Instagram item and attach its sanitized caption."""

    if not files:
        raise ValueError("No files to upload")

    del source_kind  # Reserved for future per-type formatting.

    caption = sanitize_caption(caption)
    media_groups = list(chunks(files, 10))
    caption_sent = False

    for index, group in enumerate(media_groups):
        media: list[InputMediaPhoto | InputMediaVideo] = []

        for path in group:
            item = make_media(path)

            if not caption_sent and caption:
                item.caption = caption[:1024]
                caption_sent = True

            media.append(item)

        await _send_with_retry(
            lambda media=media: bot.send_media_group(
                chat_id=chat_id,
                media=media,
            ),
            max_retries=max_retries,
        )

        if upload_delay and index < len(media_groups) - 1:
            await asyncio.sleep(upload_delay)

    # Telegram media captions have a 1024-character limit.
    # Send the remaining sanitized text as a normal message.
    if caption and len(caption) > 1024:
        await _send_with_retry(
            lambda: bot.send_message(chat_id, caption[1024:]),
            max_retries=max_retries,
        )
