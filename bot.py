"""Telegram command layer for the Instagram → Telegram bot."""

from __future__ import annotations

import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command
from aiogram.types import BotCommand, BotCommandScopeChat, Message

from config import load_settings
from database import Database
from instagram import InstagramClient
from monitor import Monitor

# =============================================================================
# 📝 Logging
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("instagram_channel_bot")


# =============================================================================
# 🚀 Application setup
# =============================================================================

settings = load_settings()
db = Database(settings.mongodb_uri, settings.mongodb_db)
instagram = InstagramClient(
    settings.downloads_dir,
    login_username=settings.ig_login_username,
    session_file=settings.ig_session_file,
)
bot = Bot(settings.bot_token)
dispatcher = Dispatcher()
router = Router()
dispatcher.include_router(router)

monitor = Monitor(
    bot,
    db,
    instagram,
    interval=settings.post_time_seconds,
    fetch_limit=settings.fetch_limit,
    profile_delay=settings.monitor_profile_delay,
    allpost_item_delay=settings.allpost_item_delay,
    allpost_batch_size=settings.allpost_batch_size,
    allpost_batch_pause=settings.allpost_batch_pause,
    telegram_upload_delay=settings.telegram_upload_delay,
    max_retries=settings.max_retries,
    error_alert_cooldown=settings.error_alert_cooldown,
    admin_ids=settings.admin_ids,
)


# =============================================================================
# 🔎 Input parsing
# =============================================================================

USERNAME_RE = re.compile(
    r"^(?:@|https?://(?:www\.)?instagram\.com/)?"
    r"([A-Za-z0-9._]+)/?(?:\?.*)?$",
    re.IGNORECASE,
)

USAGE_ADD = "/add_insta @username -1001234567890 [post|reel|both] [label]"
USAGE_SET = "/set_insta @username -1001234567890 post|reel|both [label]"
USAGE_ALLPOST = "/allpost @username -1001234567890 [post|reel|both]"
USAGE_TIME = "/set_time 30m"


def is_admin(message: Message) -> bool:
    """Check immutable Telegram numeric user ID, never username."""

    return bool(
        message.from_user
        and message.from_user.id in settings.admin_ids
    )


def require_admin(message: Message) -> bool:
    """Allow admin commands only to configured admins in private chat."""

    if message.chat.type == "private" and is_admin(message):
        return True

    # Keep all operational details private and do not answer in groups.
    if message.chat.type == "private":
        asyncio.create_task(message.answer("🔒 Private admin bot."))
    return False


def command_args(message: Message) -> list[str]:
    """Return arguments after the Telegram command."""

    text = (message.text or "").strip()
    parts = text.split()
    return parts[1:]


def parse_username(value: str) -> str:
    """Accept @username or a normal Instagram profile URL."""

    match = USERNAME_RE.match(value.strip())
    if not match:
        raise ValueError("Invalid Instagram username or profile URL")
    return match.group(1).lower()


def parse_chat_id(value: str) -> int:
    """Parse a Telegram channel/supergroup numeric chat ID."""

    try:
        chat_id = int(value)
    except ValueError as exc:
        raise ValueError("Channel ID must look like -1001234567890") from exc

    if not str(chat_id).startswith("-100"):
        log.warning("Chat ID %s does not look like a normal channel ID", chat_id)

    return chat_id


def parse_mode(value: str | None, default: str = "post") -> str:
    mode = (value or default).lower()
    if mode not in Database.VALID_MODES:
        raise ValueError("Mode must be post, reel or both")
    return mode


async def ensure_channel_access(chat_id: int) -> None:
    """Verify that this bot can post to the selected Telegram channel."""

    me = await bot.get_me()
    member = await bot.get_chat_member(chat_id, me.id)
    status = getattr(member, "status", "")
    can_post = getattr(member, "can_post_messages", True)

    if status not in {"administrator", "creator"} or can_post is False:
        raise ValueError(
            "Bot must be a channel administrator with permission to post messages."
        )


# =============================================================================
# 📋 Private admin command menu
# =============================================================================


async def install_command_menu() -> None:
    """Show commands only inside configured admin private chats."""

    commands = [
        BotCommand(command="admin_help", description="Show admin help"),
        BotCommand(command="add_insta", description="Add Instagram → channel"),
        BotCommand(command="rem_insta", description="Remove Instagram route"),
        BotCommand(command="set_insta", description="Change POST/REEL/BOTH mode"),
        BotCommand(command="list_insta", description="List monitored routes"),
        BotCommand(command="status_insta", description="Show bot status"),
        BotCommand(command="test_insta", description="Upload latest item once"),
        BotCommand(command="allpost", description="Upload profile in background"),
        BotCommand(command="jobs", description="Show /allpost jobs"),
        BotCommand(command="cancel_allpost", description="Cancel an /allpost job"),
        BotCommand(command="pause_monitor", description="Pause auto monitoring"),
        BotCommand(command="resume_monitor", description="Resume auto monitoring"),
        BotCommand(command="set_time", description="Change monitor interval"),
        BotCommand(command="ping", description="Check bot health"),
        BotCommand(command="myid", description="Show your Telegram ID"),
    ]

    await bot.delete_my_commands()

    for admin_id in settings.admin_ids:
        try:
            await bot.set_my_commands(
                commands,
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception:
            log.exception(
                "Could not install command menu for admin %s",
                admin_id,
            )


def parse_time_seconds(value: str) -> int:
    """Parse 30m, 1h, 2h, 1d or a plain minute value."""

    raw = value.strip().lower()
    match = re.fullmatch(r"(\d+)\s*(s|m|h|d)?", raw)
    if not match:
        raise ValueError("Use 30m, 1h, 2h, 1d or a plain minute value")

    number = int(match.group(1))
    unit = match.group(2) or "m"
    seconds = number * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]

    if seconds < 300:
        raise ValueError("Minimum monitor time is 5 minutes")
    if seconds > 7 * 86400:
        raise ValueError("Maximum monitor time is 7 days")
    return seconds


def format_duration(seconds: int) -> str:
    """Format seconds into a simple admin-facing duration."""

    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{seconds // 60}m"


# =============================================================================
# 🏠 Basic commands
# =============================================================================


@router.message(Command("start"))
async def start(message: Message) -> None:
    if not require_admin(message):
        return
    await message.answer(
        "✅ <b>Instagram → Telegram Bot</b> is ready.\n\n"
        "Use /admin_help to see all commands.",
        parse_mode="HTML",
    )


@router.message(Command("admin_help"))
async def admin_help(message: Message) -> None:
    if not require_admin(message):
        return

    await message.answer(
        "🔐 <b>Admin Commands</b>\n\n"
        f"<code>/add_insta @user -1001234567890 [post|reel|both] [label]</code>\n"
        f"<code>/rem_insta @user [channel_id]</code>\n"
        f"<code>{USAGE_SET}</code>\n"
        "<code>/list_insta</code>\n"
        "<code>/status_insta</code>\n"
        "<code>/test_insta @user -1001234567890</code>\n"
        f"<code>{USAGE_ALLPOST}</code>\n"
        "<code>/jobs</code>\n"
        "<code>/cancel_allpost JOB_ID</code>\n"
        "<code>/pause_monitor</code> / <code>/resume_monitor</code>\n"
        f"<code>{USAGE_TIME}</code> — 30m, 1h, 2h\n"
        "<code>/ping</code> / <code>/myid</code>\n\n"
        "💡 Each Instagram → channel route is independent, so multiple channels are supported.",
        parse_mode="HTML",
    )


@router.message(Command("ping"))
async def ping(message: Message) -> None:
    if not require_admin(message):
        return

    try:
        await db.client.admin.command("ping")
        mongo = "OK"
    except Exception:
        mongo = "ERROR"

    await message.answer(
        f"🏓 <b>Pong</b>\nMongoDB: <b>{mongo}</b>\n"
        f"Monitor: <b>{'ON' if db.is_monitor_enabled() else 'PAUSED'}</b>\n"
        f"Interval: <b>{format_duration(db.get_monitor_interval(settings.post_time_seconds))}</b>",
        parse_mode="HTML",
    )


@router.message(Command("myid"))
async def myid(message: Message) -> None:
    if not require_admin(message):
        return
    await message.answer(
        f"🆔 Your Telegram user ID: <code>{message.from_user.id}</code>",
        parse_mode="HTML",
    )


# =============================================================================
# 📡 Feed management
# =============================================================================


@router.message(Command("add_insta"))
async def add_insta(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if not 2 <= len(args) <= 4:
        await message.answer(f"Usage: <code>{USAGE_ADD}</code>", parse_mode="HTML")
        return

    try:
        username = parse_username(args[0])
        chat_id = parse_chat_id(args[1])
        mode = parse_mode(args[2] if len(args) >= 3 else None)
        label = " ".join(args[3:])[:80] if len(args) >= 4 else ""

        # Validate profile and channel before writing the route to MongoDB.
        async with monitor._ig_lock:
            posts = await asyncio.to_thread(
                instagram.latest_posts,
                username,
                1,
            )
        if not posts:
            raise ValueError("Instagram profile has no readable posts")

        await ensure_channel_access(chat_id)

        added = db.add_feed(
            username,
            chat_id,
            mode=mode,
            label=label,
        )

        if not added:
            await message.answer(
                f"ℹ️ <b>@{username}</b> is already mapped to <code>{chat_id}</code>.",
                parse_mode="HTML",
            )
            return

        newest = posts[0]
        db.mark_initial(
            username,
            chat_id,
            newest.shortcode,
            newest.date_utc.isoformat() if newest.date_utc else None,
        )

        await message.answer(
            "✅ <b>Instagram route added</b>\n\n"
            f"Profile: <b>@{username}</b>\n"
            f"Channel: <code>{chat_id}</code>\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Label: <b>{label or 'none'}</b>\n"
            f"Monitor: every <b>{format_duration(db.get_monitor_interval(settings.post_time_seconds))}</b>\n\n"
            "The current newest item is marked as seen, so old content will not be reposted automatically.",
            parse_mode="HTML",
        )

    except Exception as exc:
        log.exception("/add_insta failed")
        await message.answer(
            f"❌ <b>Add failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


@router.message(Command("rem_insta"))
async def rem_insta(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if len(args) not in {1, 2}:
        await message.answer(
            "Usage: <code>/rem_insta @username [channel_id]</code>",
            parse_mode="HTML",
        )
        return

    try:
        username = parse_username(args[0])
        chat_id = parse_chat_id(args[1]) if len(args) == 2 else None
        removed = db.remove_feed(username, chat_id)

        await message.answer(
            f"✅ Removed <b>{removed}</b> route(s) for <b>@{username}</b>.",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(
            f"❌ <b>Remove failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


@router.message(Command("set_insta"))
async def set_insta(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if not 3 <= len(args) <= 4:
        await message.answer(f"Usage: <code>{USAGE_SET}</code>", parse_mode="HTML")
        return

    try:
        username = parse_username(args[0])
        chat_id = parse_chat_id(args[1])
        mode = parse_mode(args[2])
        label = " ".join(args[3:]) if len(args) == 4 else None

        if not db.get_feed(username, chat_id):
            raise ValueError("This Instagram profile is not mapped to that channel")

        db.set_mode(username, chat_id, mode)
        if label is not None:
            db.set_label(username, chat_id, label)

        await message.answer(
            "✅ <b>Route updated</b>\n"
            f"@{username} → <code>{chat_id}</code>\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Label: <b>{label if label is not None else 'unchanged'}</b>",
            parse_mode="HTML",
        )
    except Exception as exc:
        await message.answer(
            f"❌ <b>Update failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


@router.message(Command("list_insta"))
async def list_insta(message: Message) -> None:
    if not require_admin(message):
        return

    feeds = db.list_feeds()
    if not feeds:
        await message.answer("📭 No Instagram routes are configured yet.")
        return

    lines = ["📡 <b>Instagram Routes</b>", ""]
    for index, feed in enumerate(feeds, start=1):
        mode = feed.get("mode", "post").upper()
        label = feed.get("label") or "-"
        last = feed.get("last_shortcode") or "-"
        error = feed.get("last_error")

        lines.append(
            f"<b>{index}.</b> @{feed['username']} → <code>{feed['chat_id']}</code>\n"
            f"   Mode: <b>{mode}</b> | Label: <b>{label}</b> | Last: <code>{last}</code>"
        )
        if error:
            lines.append(f"   ⚠️ <code>{str(error)[:160]}</code>")
        lines.append("")

    await message.answer("\n".join(lines), parse_mode="HTML")


# =============================================================================
# 🩺 Status / test
# =============================================================================


@router.message(Command("status_insta"))
async def status_insta(message: Message) -> None:
    if not require_admin(message):
        return

    feeds = db.list_feeds()
    jobs = db.list_jobs(50)
    queued = sum(job.get("status") == "queued" for job in jobs)
    running = sum(job.get("status") == "running" for job in jobs)
    errors = sum(bool(feed.get("last_error")) for feed in feeds)

    await message.answer(
        "📊 <b>Bot Status</b>\n\n"
        "Process: <b>ONLINE</b>\n"
        f"Monitor: <b>{'ON' if db.is_monitor_enabled() else 'PAUSED'}</b>\n"
        f"Interval: <b>{format_duration(db.get_monitor_interval(settings.post_time_seconds))}</b>\n"
        f"Routes: <b>{len(feeds)}</b>\n"
        f"Route errors: <b>{errors}</b>\n"
        f"/allpost queued/running: <b>{queued}/{running}</b>\n"
        f"Recent fetch window: <b>{settings.fetch_limit}</b>\n"
        f"MongoDB: <code>{settings.mongodb_db}</code>",
        parse_mode="HTML",
    )


@router.message(Command("test_insta"))
async def test_insta(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if len(args) != 2:
        await message.answer(
            "Usage: <code>/test_insta @username -1001234567890</code>",
            parse_mode="HTML",
        )
        return

    try:
        username = parse_username(args[0])
        chat_id = parse_chat_id(args[1])
        await ensure_channel_access(chat_id)
        shortcode = await monitor.publish_latest(username, chat_id)
        await message.answer(
            f"✅ Test upload sent. Post: <code>{shortcode}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("/test_insta failed")
        await message.answer(
            f"❌ <b>Test failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


# =============================================================================
# 📦 Background /allpost jobs
# =============================================================================


@router.message(Command("allpost"))
async def allpost(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if len(args) not in {2, 3}:
        await message.answer(f"Usage: <code>{USAGE_ALLPOST}</code>", parse_mode="HTML")
        return

    try:
        username = parse_username(args[0])
        chat_id = parse_chat_id(args[1])
        mode = parse_mode(args[2] if len(args) == 3 else None, default="both")

        async with monitor._ig_lock:
            posts = await asyncio.to_thread(
                instagram.latest_posts,
                username,
                1,
            )
        if not posts:
            raise ValueError("Instagram profile has no readable posts")

        await ensure_channel_access(chat_id)

        job_id = db.create_allpost_job(
            username,
            chat_id,
            message.from_user.id,
            mode=mode,
        )

        await message.answer(
            "✅ <b>/allpost queued</b>\n\n"
            f"Profile: <b>@{username}</b>\n"
            f"Channel: <code>{chat_id}</code>\n"
            f"Mode: <b>{mode.upper()}</b>\n"
            f"Job: <code>{job_id}</code>\n\n"
            "The bot remains responsive. Use /jobs to check progress.",
            parse_mode="HTML",
        )

    except Exception as exc:
        log.exception("/allpost command failed")
        await message.answer(
            f"❌ <b>Allpost failed to queue</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


@router.message(Command("jobs"))
async def jobs(message: Message) -> None:
    if not require_admin(message):
        return

    rows = db.list_jobs(15)
    if not rows:
        await message.answer("📭 No /allpost jobs yet.")
        return

    lines = ["🧾 <b>Recent /allpost Jobs</b>", ""]
    for job in rows:
        lines.append(
            f"• <code>{job['_id']}</code> | <b>{job.get('status', '?').upper()}</b>\n"
            f"  @{job.get('username')} → <code>{job.get('chat_id')}</code> | "
            f"OK={job.get('processed', 0)} FAIL={job.get('failed', 0)}\n"
            f"  Last: <code>{job.get('last_shortcode') or '-'}</code>"
        )

    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("cancel_allpost"))
async def cancel_allpost(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if len(args) != 1:
        await message.answer(
            "Usage: <code>/cancel_allpost JOB_ID</code>",
            parse_mode="HTML",
        )
        return

    cancelled = db.cancel_job(args[0])
    await message.answer(
        "🛑 Job cancelled." if cancelled else "❌ Job not found or already finished.",
    )


@router.message(Command("set_time"))
async def set_time(message: Message) -> None:
    if not require_admin(message):
        return

    args = command_args(message)
    if len(args) != 1:
        await message.answer(
            f"Usage: <code>{USAGE_TIME}</code>\n\n"
            "Examples: <code>/set_time 30</code>, <code>/set_time 30m</code>, "
            "<code>/set_time 1h</code>, <code>/set_time 1800s</code>",
            parse_mode="HTML",
        )
        return

    try:
        seconds = parse_time_seconds(args[0])
        db.set_monitor_interval(seconds)
        monitor.wake()
        await message.answer(
            "✅ <b>Monitor time updated</b>\n\n"
            f"New interval: <b>{format_duration(seconds)}</b> ({seconds}s)\n\n"
            "Saved in MongoDB. No restart is required.",
            parse_mode="HTML",
        )
    except Exception as exc:
        log.exception("/set_time failed")
        await message.answer(
            f"❌ <b>Time update failed</b>\n<code>{type(exc).__name__}: {exc}</code>",
            parse_mode="HTML",
        )


# =============================================================================
# ⏯ Monitor controls
# =============================================================================


@router.message(Command("pause_monitor"))
async def pause_monitor(message: Message) -> None:
    if not require_admin(message):
        return
    db.set_monitor_enabled(False)
    await message.answer(
        "⏸️ Automatic Instagram monitoring is paused.\n"
        "Use /resume_monitor to enable it again."
    )


@router.message(Command("resume_monitor"))
async def resume_monitor(message: Message) -> None:
    if not require_admin(message):
        return
    db.set_monitor_enabled(True)
    await message.answer("▶️ Automatic Instagram monitoring resumed.")


# =============================================================================
# 🔒 Private fallback
# =============================================================================


@router.message()
async def private_fallback(message: Message) -> None:
    """Do not expose bot functionality to non-admin users."""

    if not is_admin(message) and message.chat.type == "private":
        await message.answer("🔒 Private admin bot.")


# =============================================================================
# 🏁 Entrypoint
# =============================================================================


async def main() -> None:
    """Start polling and gracefully shut down all resources."""

    await install_command_menu()
    monitor_task = asyncio.create_task(
        monitor.run(),
        name="instagram-background-workers",
    )

    try:
        await dispatcher.start_polling(bot)
    finally:
        await monitor.stop()
        await monitor_task
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
