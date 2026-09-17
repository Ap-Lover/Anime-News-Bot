# Instagram → Telegram Bot — PRD + Deep Audit / Fix Prompt

> Paste this entire document into Cline/another coding agent. The goal is to deeply audit the repository, fix concrete problems, preserve the existing architecture, and return a production/GitHub-ready result.

## 1. Product goal

Build a **private, lightweight, production-ready Telegram bot** that monitors one or more Instagram profiles and publishes newly detected Instagram media to one or more Telegram channels.

The project must remain beginner-friendly. Keep configuration obvious, comments useful, module responsibilities clear, filenames predictable, and abstractions minimal.

## 2. Existing architecture — DO NOT redesign casually

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

Preserve this flow when editing. Only change the structure when a concrete bug, security issue, dependency compatibility problem, or measurable resource issue requires it. Document any necessary structural exception.

### Module responsibilities

- `config.py` → environment variables, defaults, validation, beginner-readable settings.
- `bot.py` → Telegram command handlers, private/admin authorization, command parsing and admin-facing messages.
- `database.py` → MongoDB only: collections, indexes, routes, delivery markers, settings, jobs.
- `instagram.py` → Instaloader wrapper: profile/post retrieval, Reel classification, downloading and temporary-file cleanup.
- `monitor.py` → 24/7 scheduler, unique-profile grouping, route filtering, `/allpost` background worker, recovery and coordination.
- `uploader.py` → Telegram media upload, media groups, retry handling and caption sanitization.

Do not move responsibilities between these modules simply for aesthetics.

## 3. Configuration / beginner UX

`config.py` must be organized in obvious sections:

```python
# Recommended
# Main
# Database
# Monitoring / Post Time
# /allpost
# Telegram Upload / Error Handling
# Instagram / Files
```

The default monitor timer must be visible in config:

```python
POST_TIME_SECONDS = 1800  # 30 minutes
```

Keep `MONITOR_INTERVAL_SECONDS` only as a backward-compatible environment alias if already supported.

Never commit real secrets. Use `.env` locally and Render/VPS environment variables in deployment.

## 4. Private admin bot

The bot is private.

Authorization must use immutable Telegram numeric `user.id`. Never use Telegram usernames or display names for authentication.

Use `OWNER_ID` plus optional comma-separated `ADMIN_IDS`.

Non-admin users must not receive route information, channel IDs, Instagram mappings, job IDs, database details, or internal errors.

Admin command set should include:

```text
/start
/admin_help
/ping
/myid

/add_insta
/rem_insta
/set_insta
/list_insta
/status_insta
/test_insta

/set_time
/pause_monitor
/resume_monitor

/allpost
/jobs
/cancel_allpost
```

Admin command menus may be scoped to admin private chats only.

## 5. Live `/set_time` requirement

Default interval: 30 minutes.

Supported examples:

```text
/set_time 30       # 30 minutes
/set_time 30m      # 30 minutes
/set_time 45m      # 45 minutes
/set_time 1h       # 1 hour
/set_time 1800s    # 30 minutes
/set_time 1d       # 1 day
```

Required behavior:

1. Validate the input.
2. Enforce a safe minimum of 5 minutes.
3. Enforce a maximum of 7 days.
4. Save the value in MongoDB.
5. Apply it without restarting the bot.
6. Show the current live value in `/status_insta`.
7. Keep the default/startup value visible in `config.py`.
8. Preserve the existing `config → bot → monitor → ... → MongoDB` flow.

The monitor should use a wake/event mechanism so a changed interval can apply immediately instead of waiting for the old timer to expire.

## 6. Instagram monitoring

Normal monitoring must be lightweight:

- Default interval 30 minutes.
- Group routes by unique Instagram profile.
- Fetch each unique profile once per cycle.
- Fetch only a small recent window during normal monitoring.
- Apply route-specific `post`, `reel`, or `both` filters.
- Check MongoDB duplicate marker before download/upload.
- Download only needed media.
- Upload sequentially.
- Save marker/delivery state.
- Delete temporary files after each item.
- Back off on transient failures.
- Never intentionally bypass Instagram rate limits.

## 7. POST / REEL / BOTH routing

Each Instagram → Telegram channel mapping is independent.

Example:

```text
Instagram A → Channel 1 → post
Instagram A → Channel 2 → reel
Instagram B → Channel 3 → both
```

Required command examples:

```text
/add_insta @animepage -1001111111111 post anime
/add_insta @moviepage -1002222222222 post movie
/add_insta @creator -1003333333333 reel reels
/add_insta @creator -1004444444444 both mixed
```

Keep Instagram type classification isolated in `instagram.py` and fail safely when metadata is incomplete.

## 8. Caption sanitization

Preserve useful Instagram caption text, but remove completely from the caption before Telegram delivery:

- `@username` mentions
- `http://...`
- `https://...`
- `ftp://...`
- `www.example.com`
- bare domains like `example.com`
- Telegram links like `t.me/channel`
- extra whitespace / duplicate blank lines

Do not claim that this removes text visually embedded inside an image or video. That is a separate media/OCR feature.

## 9. Multiple channel support

Support many independent routes.

A feed route should store at least:

```text
username
chat_id
mode
label
last_shortcode
last_post_date
last_check
last_error
created_at
updated_at
```

MongoDB indexes must keep route and duplicate lookups efficient.

## 10. `/allpost` background worker

Commands:

```text
/allpost @username -1001234567890 post
/allpost @username -1001234567890 reel
/allpost @username -1001234567890 both
```

The command must enqueue a persistent background job and immediately return control to Telegram.

Required protections:

- stream profile history; do not load the entire profile into RAM.
- one Instagram operation at a time by default.
- one active bulk job by default.
- per-item delay.
- batch pause.
- duplicate-safe deliveries.
- MongoDB progress persistence.
- cancellation.
- restart recovery.
- progress messages.
- per-item failure isolation.
- temporary file cleanup.
- no intentional rate-limit bypass.

## 11. Error handling

The bot must survive an individual bad post, download failure, upload failure or temporary network issue.

Handle at least:

- Instagram connection/temporary errors.
- Instagram access/private/nonexistent profiles.
- Instagram rate-limit responses.
- Telegram network/server errors.
- Telegram `RetryAfter`.
- Telegram permission errors.
- MongoDB startup/connectivity errors.
- invalid command arguments.
- invalid job IDs.
- empty/unsupported media.
- shutdown/cancellation.

Retries must be bounded. Permanent errors must not retry forever.

Admin alerts must be throttled to avoid spam.

## 12. Persistence

MongoDB only. No SQLite.

Collections:

```text
feeds
deliveries
jobs
settings
```

The database layer should be the only module that knows MongoDB collection names and query details.

## 13. Resource efficiency

Target:

- small VPS
- Render Background Worker
- Docker

Keep CPU/RAM/disk low by:

- sequential Instagram operations
- unique-profile grouping
- small normal-monitor fetch window
- bounded retries
- long default polling interval
- background `/allpost`
- deleting temporary media
- avoiding tight loops
- avoiding unbounded in-memory lists

## 14. Telegram compatibility

Verify the pinned `aiogram` version against the implementation. Check:

- `Bot`
- `Dispatcher`
- `Router`
- `Command`
- `BotCommand`
- `BotCommandScopeChat`
- `send_media_group`
- retry exception classes
- graceful session shutdown

Do not upgrade dependencies blindly; if versions are changed, explain why and run compatibility tests.

## 15. Instagram compatibility

Verify the pinned Instaloader version against current usage. Review:

- profile lookup
- profile iteration
- post metadata
- Reel classification
- `download_post`
- session loading
- temporary folder handling
- exception classes

Do not promise that Instagram will never rate-limit or change endpoints. The implementation should reduce request pressure and fail safely.

## 16. MongoDB review

Audit:

- client timeouts
- startup ping
- connection reuse
- indexes
- unique route index
- unique delivery index
- ObjectId parsing
- atomic state updates
- job recovery
- timer persistence
- monitor pause state

Consider whether any synchronous MongoDB operation on the asyncio event loop can materially block Telegram responsiveness. If so, fix it without destroying the current module architecture.

## 17. Async/concurrency review

Audit for:

- monitor + `/allpost` overlap
- Instagram lock usage
- duplicate publication races
- cancellation
- shutdown
- stale tasks
- task leaks
- lost wake events
- job recovery after crash

`asyncio.wait(..., FIRST_COMPLETED)` should be used only with actual `Task`/`Future` objects, and cancelled/pending tasks must be cleaned up correctly. Python 3.11 explicitly requires tasks/futures rather than raw coroutines for `asyncio.wait`. 

## 18. Security review

Scan recursively for:

- hardcoded Telegram bot tokens
- API hashes
- MongoDB credentials
- Instagram session files
- secrets inside logs
- command injection
- unsafe filesystem paths
- HTML/Markdown injection in admin messages
- admin authorization weaknesses
- accidental public command exposure

Any secret found in source must be removed and the report must identify it without reproducing the secret value.

## 19. GitHub readiness

Repository must contain:

```text
README.md
.env.example
.gitignore
requirements.txt
pyproject.toml
Dockerfile
docker-compose.yml
render.yaml
.github/workflows/ci.yml
```

Optionally include:

```text
.python-version
tests/
```

Never commit `.env`, session files, local downloads, logs, virtual environments, or credentials.

## 20. Deep scan procedure — BEFORE editing

Recursively inspect every file.

Do not stop after the first obvious bug.

Scan categories:

### A. Python
- syntax
- imports
- circular imports
- undefined names
- wrong function signatures
- bad exception classes
- blocking calls
- race conditions
- task cancellation
- resource cleanup

### B. Telegram
- aiogram API compatibility
- command handlers
- private/admin access
- channel permission checks
- media group behavior
- caption limit handling
- retry handling
- graceful shutdown

### C. Instagram
- Instaloader compatibility
- profile visibility
- post iteration
- Reels
- downloading
- cleanup
- session handling
- temporary errors
- request pressure

### D. MongoDB
- connectivity
- indexes
- persistence
- atomic updates
- ObjectId handling
- timer persistence
- job recovery

### E. Concurrency
- shared Instagram lock
- normal monitor vs bulk worker
- cancellation
- restart recovery
- duplicate delivery
- wake/event correctness

### F. Deployment
- Python 3.11 compatibility
- Docker build
- Render worker
- writable paths
- environment variables
- restart behavior

### G. UX
- readable config
- understandable command usage
- clear errors
- no hidden required settings

## 21. Fix rules

For every finding:

```text
Identify → explain root cause → patch → regression test → rerun checks → report result
```

Do not use broad exception handlers merely to hide errors.

Do not remove a requested feature silently.

Do not replace MongoDB.

Do not remove `/allpost`.

Do not remove multi-channel support.

Do not remove POST/REEL/BOTH routing.

Do not remove caption sanitization.

Do not rewrite the architecture without a concrete reason.

## 22. Required checks

Run as many as the environment allows:

```bash
python -m compileall -q .
python -m unittest discover -s tests -p "test_*.py" -v
ruff check .
```

For CI/runtime validation, also run installation and a minimal startup/import test using safe fake/missing environment variables where possible. Do not connect to a real bot or production MongoDB from CI.

Never claim a test passed if it was not actually run.

## 23. Required final report format

Return exactly these sections:

1. Executive Summary
2. Repository Structure Reviewed
3. Features Verified
4. Bugs Found
5. Bugs Fixed
6. Security Findings
7. Performance / Resource Findings
8. Telegram Compatibility Review
9. Instagram / Instaloader Compatibility Review
10. MongoDB Review
11. Concurrency / Recovery Review
12. Deployment Review — VPS / Docker / Render
13. Tests Executed
14. Tests Not Executable in Current Environment
15. Files Changed
16. Backward Compatibility Notes
17. Remaining Risks / Limitations
18. GitHub Readiness Checklist

For every bug, include:

```text
Bug:
File:
Location:
Impact:
Root cause:
Fix:
Regression test:
Status:
```

## 24. Required final deliverables

Return:

- the corrected repository
- complete audit report
- changed-file list
- test results
- environment limitations
- confirmation that `POST_TIME_SECONDS` is visible in `config.py`
- confirmation that `/set_time` stores the timer in MongoDB
- confirmation that `/set_time` applies without restart
- confirmation that multiple channels and POST/REEL/BOTH are preserved
- confirmation that `/allpost` remains a persistent background job
- confirmation that caption sanitization remains enabled
- confirmation that no secrets are committed
- confirmation that the architecture remains:

```text
config → bot → monitor → instagram/uploader → database/MongoDB
```
