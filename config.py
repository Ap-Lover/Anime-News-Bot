"""Central configuration for the Instagram → Telegram bot.

Beginners: almost everything you need to change is in this one file's
sections below. For VPS/Render, values are normally supplied as environment
variables. Do NOT put real secrets in GitHub.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env when running locally. VPS/Render environment variables still work.
load_dotenv()


# =============================================================================
# 🔐 RECOMMENDED — Telegram credentials
# =============================================================================

# Bot token from @BotFather.
TG_BOT_TOKEN = os.environ.get(
    "TG_BOT_TOKEN",
    os.environ.get("BOT_TOKEN", ""),
).strip()

# Optional Telegram API credentials.
# This project currently uses the Telegram Bot API, so these can stay blank.
APP_ID = int(os.environ.get("APP_ID", "0") or "0")
API_HASH = os.environ.get("API_HASH", "").strip()


# =============================================================================
# 👑 MAIN — Owner / Admin / App
# =============================================================================

# Your Telegram numeric user ID. This is the main owner account.
OWNER_ID = int(os.environ.get("OWNER_ID", "0") or "0")

# Optional extra admin IDs: 123456789,987654321
_EXTRA_ADMIN_IDS = os.environ.get("ADMIN_IDS", "").strip()

# Render/VPS port. The bot is a worker, so it normally does not need HTTP.
PORT = os.environ.get("PORT", "8028").strip()


# =============================================================================
# 🍃 DATABASE — MongoDB
# =============================================================================

# Preferred variable: DATABASE_URL
# Compatibility variable: DB_URI / MONGODB_URI
DB_URI = os.environ.get(
    "DATABASE_URL",
    os.environ.get("DB_URI", os.environ.get("MONGODB_URI", "")),
).strip()

# Preferred variable: DATABASE_NAME
# Compatibility variable: DB_NAME / MONGODB_DB
DB_NAME = os.environ.get(
    "DATABASE_NAME",
    os.environ.get(
        "DB_NAME",
        os.environ.get("MONGODB_DB", "instagram_channel_bot"),
    ),
).strip()


# =============================================================================
# 📡 MONITORING — lightweight 24/7 settings
# =============================================================================

# 1800 seconds = 30 minutes.
# This is the startup/default value. Admin can change it live with /set_time.
POST_TIME_SECONDS = max(
    300,
    int(
        os.environ.get(
            "POST_TIME_SECONDS",
            os.environ.get("MONITOR_INTERVAL_SECONDS", "1800"),
        )
        or "1800"
    ),
)

# Backward-compatible variable name for older deployments.
MONITOR_INTERVAL_SECONDS = POST_TIME_SECONDS

# How many recent Instagram items are checked during normal monitoring.
FETCH_LIMIT = max(
    3,
    min(20, int(os.environ.get("FETCH_LIMIT", "5") or "5")),
)

# Small delay between different Instagram profiles.
MONITOR_PROFILE_DELAY = max(
    0.0,
    float(os.environ.get("MONITOR_PROFILE_DELAY", "2") or "2"),
)


# =============================================================================
# 📦 /allpost — background bulk uploader
# =============================================================================

# Delay between processed items.
ALLPOST_ITEM_DELAY = max(
    0.0,
    float(os.environ.get("ALLPOST_ITEM_DELAY", "2") or "2"),
)

# After this many successful uploads, pause before continuing.
ALLPOST_BATCH_SIZE = max(
    1,
    min(100, int(os.environ.get("ALLPOST_BATCH_SIZE", "20") or "20")),
)

# Pause between bulk batches.
ALLPOST_BATCH_PAUSE = max(
    0.0,
    float(os.environ.get("ALLPOST_BATCH_PAUSE", "10") or "10"),
)


# =============================================================================
# 📤 TELEGRAM UPLOAD — retry / error protection
# =============================================================================

# Delay between Telegram media groups.
TELEGRAM_UPLOAD_DELAY = max(
    0.0,
    float(os.environ.get("TELEGRAM_UPLOAD_DELAY", "0.7") or "0.7"),
)

# Maximum retry attempts for temporary Telegram/Instagram failures.
MAX_RETRIES = max(
    1,
    min(5, int(os.environ.get("MAX_RETRIES", "3") or "3")),
)

# Same repeated error will not spam all admins every few minutes.
ERROR_ALERT_COOLDOWN = max(
    300,
    int(os.environ.get("ERROR_ALERT_COOLDOWN", "21600") or "21600"),
)


# =============================================================================
# 📁 FILES / INSTAGRAM SESSION
# =============================================================================

# Temporary download directory. Files are deleted after upload.
DOWNLOADS_DIR = os.environ.get(
    "DOWNLOADS_DIR",
    "data/downloads",
).strip()

# Optional authenticated Instaloader session.
# Leave blank for public-profile monitoring.
IG_LOGIN_USERNAME = os.environ.get("IG_LOGIN_USERNAME", "").strip()
IG_SESSION_FILE = os.environ.get("IG_SESSION_FILE", "").strip()


# =============================================================================
# 🧩 Runtime settings object
# =============================================================================


@dataclass(frozen=True)
class Settings:
    """Validated settings used by the rest of the application."""

    bot_token: str
    admin_ids: frozenset[int]
    post_time_seconds: int
    mongodb_uri: str
    mongodb_db: str
    downloads_dir: str
    ig_login_username: str
    ig_session_file: str
    fetch_limit: int
    monitor_profile_delay: float
    allpost_item_delay: float
    allpost_batch_size: int
    allpost_batch_pause: float
    telegram_upload_delay: float
    error_alert_cooldown: int
    max_retries: int
    port: str
    app_id: int
    api_hash: str


def _parse_admin_ids() -> frozenset[int]:
    """Combine OWNER_ID and optional ADMIN_IDS into one immutable set."""

    admin_ids: set[int] = set()

    if OWNER_ID:
        admin_ids.add(OWNER_ID)

    if _EXTRA_ADMIN_IDS:
        raw_values = _EXTRA_ADMIN_IDS.replace(" ", "").split(",")
        for raw_id in raw_values:
            if not raw_id:
                continue
            try:
                admin_ids.add(int(raw_id))
            except ValueError as exc:
                raise RuntimeError(
                    "ADMIN_IDS must contain comma-separated Telegram numeric IDs."
                ) from exc

    if not admin_ids:
        raise RuntimeError(
            "OWNER_ID or ADMIN_IDS is required. "
            "Example: OWNER_ID=123456789"
        )

    return frozenset(admin_ids)


def load_settings() -> Settings:
    """Validate the environment and return one clean runtime configuration."""

    if not TG_BOT_TOKEN:
        raise RuntimeError(
            "TG_BOT_TOKEN is required. Add it to .env, VPS or Render variables."
        )

    if not DB_URI:
        raise RuntimeError(
            "DATABASE_URL is required. Add your MongoDB connection string."
        )

    Path(DOWNLOADS_DIR).mkdir(parents=True, exist_ok=True)

    return Settings(
        bot_token=TG_BOT_TOKEN,
        admin_ids=_parse_admin_ids(),
        post_time_seconds=POST_TIME_SECONDS,
        mongodb_uri=DB_URI,
        mongodb_db=DB_NAME,
        downloads_dir=DOWNLOADS_DIR,
        ig_login_username=IG_LOGIN_USERNAME,
        ig_session_file=IG_SESSION_FILE,
        fetch_limit=FETCH_LIMIT,
        monitor_profile_delay=MONITOR_PROFILE_DELAY,
        allpost_item_delay=ALLPOST_ITEM_DELAY,
        allpost_batch_size=ALLPOST_BATCH_SIZE,
        allpost_batch_pause=ALLPOST_BATCH_PAUSE,
        telegram_upload_delay=TELEGRAM_UPLOAD_DELAY,
        error_alert_cooldown=ERROR_ALERT_COOLDOWN,
        max_retries=MAX_RETRIES,
        port=PORT,
        app_id=APP_ID,
        api_hash=API_HASH,
    )
