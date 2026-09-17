# Deep Audit Report — Instagram → Telegram Bot

## Audit Scope

Repository audited: Instagram → Telegram Auto Uploader (V5 baseline).

Audit included configuration, Telegram command layer, MongoDB persistence, Instagram wrapper, monitor/background worker, uploader/caption cleaner, Docker, Render configuration, GitHub hygiene, tests, and async/concurrency flow.

## 1. Executive Summary

The baseline architecture was already clear and modular, but the live monitor timer requested by the project was not implemented. The timer was startup-only, meaning changing an environment variable or adding a command could not update the running worker without a restart.

The final repository adds a MongoDB-backed live timer with `/set_time`, keeps the default timer visible in `config.py`, adds a wake-up path so the new interval can apply without restarting, adds regression tests, adds GitHub Actions CI, and keeps the existing module flow intact.

The repository is GitHub-ready from a file hygiene/structure perspective, but full live runtime validation was not possible in this environment because the required runtime packages and external Telegram/MongoDB/Instagram services were not available.

## 2. Original Architecture Reviewed

```text
config.py
   ↓
bot.py ─────────────────┐
   ↓                     │
monitor.py               │
   ↓                     │
instagram.py             │
   ↓                     │
uploader.py              │
   ↓                     │
database.py ←─────────────┘
   ↓
MongoDB
```

This architecture was preserved.

## 3. Features Verified

- Private/admin-only command layer.
- Numeric Telegram `user.id` based authorization.
- MongoDB-only persistence.
- Multiple Instagram → Telegram routes.
- Per-route POST / REEL / BOTH mode.
- Caption cleaning for mentions and website links.
- Carousel/media-group upload support.
- Duplicate delivery markers.
- 30-minute default monitor interval.
- Background `/allpost` worker.
- Job persistence and recovery markers.
- Temporary media cleanup.
- Telegram retry handling.
- Admin error-alert throttling.
- Docker deployment.
- Render Background Worker deployment.
- Beginner-readable configuration and README.

## 4. Bug Found and Fixed

### Bug: Monitor interval could not be changed live

**File:** `config.py`, `monitor.py`, `bot.py`, `database.py`

**Impact:** The monitoring interval was loaded at process startup. A running bot had no supported command to change its 30-minute schedule without restarting.

**Root cause:** There was no runtime/persistent timer setting in MongoDB and the monitor loop only used the constructor's startup interval.

**Fix:**

- Added `POST_TIME_SECONDS` as the visible startup/default setting in `config.py`.
- Kept `MONITOR_INTERVAL_SECONDS` as a backward-compatible environment alias.
- Added MongoDB `settings.monitor_interval_seconds` persistence.
- Added `/set_time` command.
- Added parsing for `30`, `30m`, `45m`, `1h`, `1800s`, `1d`.
- Enforced 5-minute minimum and 7-day maximum.
- Added a monitor wake event so the new interval can be applied without restart.
- Added live interval output to status/route messages.

### Example

```text
/set_time 30
/set_time 30m
/set_time 45m
/set_time 1h
/set_time 1800s
```

## 5. Additional Reliability Findings

### Synchronous MongoDB calls inside the asyncio application

The database layer uses synchronous PyMongo operations while the Telegram framework is asynchronous. This is functional for a low-frequency/light workload, but a slow MongoDB network operation could temporarily block the asyncio event loop.

**Decision:** Not migrated in this pass in order to preserve the existing module architecture and minimize unrelated changes. This remains a performance optimization candidate.

Current MongoDB connection pool is deliberately small (`maxPoolSize=8`, `minPoolSize=0`) to limit resource usage.

MongoDB now provides a native `AsyncMongoClient`; MongoDB's current documentation recommends it for asyncio applications when asynchronous driver behavior is desired. citehttps://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/mongoclient/

### Normal-monitor fetch window limitation

The normal monitor intentionally checks only a small recent window. If the saved marker falls outside that window, the bot does not backfill the entire missed range. This prevents unexpected large reposts and keeps monitoring light.

Use `/allpost` for deliberate historical/backfill uploads.

### Crash boundary / at-least-once delivery

A rare crash can occur after Telegram accepts a media upload but before the MongoDB delivery marker is saved. That can produce a single duplicate after restart. MongoDB duplicate markers prevent normal repeated deliveries, but exactly-once delivery across two separate external systems cannot be guaranteed with the current architecture.

### Instagram access dependency

The bot uses Instaloader for third-party/public-profile access. Instaloader is an independent unofficial project and Instagram can change endpoints, authentication requirements, or rate-limit automated access. The project reduces request pressure but intentionally does not attempt to bypass platform limits.

## 6. Security Findings

- No obvious hardcoded Telegram bot-token pattern was found in source/config files.
- No obvious hardcoded MongoDB credential pattern was found in source/config files.
- `.env`, session files, logs and downloaded media are ignored by `.gitignore`.
- Admin authorization uses numeric Telegram IDs.
- Admin command menus are scoped to configured admin private chats.
- `/set_time` is admin-protected.

The real credentials previously pasted into chat should be treated as exposed and rotated before production use.

## 7. Performance / Resource Findings

Resource-saving mechanisms already present and retained:

- 30-minute default monitoring.
- Unique-profile grouping during a monitor cycle.
- Small normal fetch window.
- One-at-a-time Instagram operation lock.
- Streaming `/allpost` iteration.
- One active bulk worker.
- Per-item and batch delays.
- Bounded retry counts.
- Temporary media cleanup after upload.
- Small MongoDB connection pool.

## 8. Telegram Compatibility Review

Pinned `aiogram==3.31.0` exists on PyPI and was uploaded on August 26, 2026. Its Bot API layer and `setMyCommands`/chat-scoped command support match the project's command-menu design. citehttps://pypi.org/project/aiogram/3.31.0/ citehttps://docs.aiogram.dev/en/v3.31.0/_modules/aiogram/methods/set_my_commands.html

The code uses `BotCommandScopeChat` for private admin command menus. citehttps://docs.aiogram.dev/en/v3.31.0/api/types/bot_command_scope_chat.html

Live Telegram API execution was not run in this environment.

## 9. Instagram / Instaloader Compatibility Review

Pinned `instaloader==4.15.3` exists on PyPI and was released July 26, 2026. It supports Python 3.11 and documents profile/post downloading and caption handling. citehttps://pypi.org/project/instaloader/4.15.3/

Live Instagram profile access was not run in this environment.

## 10. MongoDB Review

The project uses PyMongo only and does not use SQLite.

Collections:

```text
feeds
deliveries
jobs
settings
```

Important indexes are created for route uniqueness, delivery uniqueness, and job queue ordering.

Current PyMongo documentation supports both synchronous and native asynchronous APIs; the async API is a future optimization path if event-loop blocking becomes measurable. citehttps://www.mongodb.com/docs/languages/python/pymongo-driver/current/connect/

Live Atlas/MongoDB execution was not run in this environment.

## 11. Concurrency / Recovery Review

- Normal monitoring and `/allpost` share a single Instagram lock.
- `/allpost` iterates via `asyncio.to_thread` so blocking Instaloader iteration does not block the Telegram event loop.
- Bulk jobs are persistent in MongoDB.
- Running jobs are recovered to queued state after process restart.
- `/cancel_allpost` is persisted and checked between items.
- The new `/set_time` event wakes the monitor loop.

Python 3.11 documents `asyncio.wait` with `FIRST_COMPLETED` for Task/Future objects and confirms pending tasks are returned rather than implicitly cancelled on timeout; the implementation explicitly cleans up pending timer tasks. citehttps://docs.python.org/3.11/library/asyncio-task.html

## 12. Deployment Review

### VPS

Docker and plain Python startup paths are provided. Temporary files are stored under `data/downloads` and removed after use.

### Docker

`Dockerfile` uses Python 3.11 and starts `python bot.py`.

### Render

`render.yaml` defines a Background Worker and now exposes `POST_TIME_SECONDS=1800` as the default environment value.

## 13. Tests Executed

### Passed

- Python recursive compile: PASS.
- Static repository checks: PASS.
- Timer parser regression tests: PASS.
- GitHub-ready file presence checks: PASS.
- Source secret-pattern scan: PASS.

### Skipped

Caption sanitizer tests were skipped locally because `aiogram` is not installed in the current execution environment. The GitHub CI workflow installs project dependencies first, so the caption tests will execute in CI.

### Not executed

- Live Telegram API test.
- Live MongoDB/Atlas test.
- Live Instagram profile download test.
- Ruff lint run locally (Ruff is not installed in the current execution environment).

## 14. Files Added/Changed From V5

### Changed

```text
.env.example
README.md
bot.py
config.py
database.py
monitor.py
render.yaml
```

### Added

```text
.github/workflows/ci.yml
.python-version
PRD_AND_DEEP_AUDIT_PROMPT.md
tests/__init__.py
tests/test_static.py
tests/test_caption_sanitizer.py
DEEP_AUDIT_REPORT.md
```

## 15. GitHub Readiness

### Ready

- No production secrets in source files.
- `.env` ignored.
- Session files ignored.
- Download directory ignored.
- GitHub Actions CI included.
- Docker and Render definitions included.
- Beginner README included.
- Default timer visible in `config.py`.
- `/set_time` included in command menu and help.
- MongoDB persistence retained.

### Human decision still needed

Choose and add an open-source `LICENSE` before publishing, if the repository is intended to be redistributed.

## 16. Remaining Risks / Limitations

1. Instagram's unofficial/public-profile access can break or be rate-limited.
2. Exactly-once delivery cannot be guaranteed at the Telegram/MongoDB crash boundary.
3. Normal monitoring intentionally uses a recent window and does not automatically backfill large gaps.
4. Synchronous PyMongo remains a possible event-loop blocking point if MongoDB becomes slow.
5. Full production validation still requires safe test credentials and accessible external services.
