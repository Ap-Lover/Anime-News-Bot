# 📸 Instagram → Telegram Auto Uploader

A private, lightweight Telegram bot that monitors Instagram profiles and automatically publishes new media to one or many Telegram channels.

> **Designed for beginners:** configuration is grouped in `config.py`, secrets live in environment variables, and every Python file has one clear job.

---

## ✨ What it does

```text
Instagram Profile
      │
      ▼
30-minute lightweight monitor
      │
      ├── POST ✅
      ├── REEL ✅
      └── BOTH ✅
      │
      ▼
Caption cleaner
(remove @usernames + links)
      │
      ▼
Telegram channel
```

### Main features

- 🍃 MongoDB only — no SQLite
- ⏱️ 30-minute monitoring by default
- 🕒 Live monitor timer control with `/set_time` (no restart required)
- 📡 One Instagram fetch per unique profile per monitor cycle
- 🖼️ Photos, videos and carousels
- 📝 Original caption preserved after cleaning
- 🧹 Removes `@username`, `https://...`, `http://...`, `www...` and bare domain links from captions
- 🔁 Duplicate protection in MongoDB
- 🔐 Private admin-only bot using Telegram numeric `user.id`
- 📚 Multiple Instagram → Telegram routes
- 🎛️ Per-route `post`, `reel` or `both` control
- 🏷️ Optional route labels such as `anime` / `movie`
- 📦 `/allpost` runs as a persistent background job
- ⏯️ Pause/resume monitoring without stopping the bot
- ♻️ Restart recovery for interrupted `/allpost` jobs
- 🧯 Error handling, retry/backoff and throttled admin alerts
- 🧹 Temporary media cleanup to keep VPS/Render storage light
- 🐳 Docker / VPS ready
- ☁️ Render Background Worker ready

> Instagram access can still be rate-limited or changed by Instagram. The bot minimizes request pressure; it does not bypass platform limits.

---

## 📁 Simple project structure

```text
instagram_channel_bot/
│
├── bot.py             ← Telegram commands + admin controls
├── config.py          ← ALL important settings
├── database.py        ← MongoDB storage
├── instagram.py       ← Instagram/Instaloader wrapper
├── monitor.py         ← 24/7 monitor + /allpost worker
├── uploader.py        ← Caption cleaning + Telegram upload
│
├── .env.example       ← Copy to .env and fill your values
├── requirements.txt    ← Python dependencies
├── Dockerfile
├── docker-compose.yml
├── render.yaml
├── pyproject.toml
├── .gitignore
└── README.md
```

### How the files connect

```text
config.py
   ↓
bot.py ───────────────┐
   ↓                   │
monitor.py             │
   ↓                   │
instagram.py           │
   ↓                   │
uploader.py            │
   ↓                   │
database.py ←──────────┘
   ↓
MongoDB
```

---

# ⚙️ Configuration

Open **`config.py`** first. It is deliberately organized into simple sections:

```python
# Recommended
TG_BOT_TOKEN = ...
APP_ID = ...
API_HASH = ...

# Main
OWNER_ID = ...
PORT = ...

# Database
DB_URI = ...
DB_NAME = ...

# Monitoring / Post Time
POST_TIME_SECONDS = 1800  # 30 minutes
FETCH_LIMIT = ...

# /allpost
ALLPOST_ITEM_DELAY = ...
ALLPOST_BATCH_SIZE = ...
ALLPOST_BATCH_PAUSE = ...

# Telegram / error handling
TELEGRAM_UPLOAD_DELAY = ...
MAX_RETRIES = ...
ERROR_ALERT_COOLDOWN = ...

# Instagram / files
IG_LOGIN_USERNAME = ...
IG_SESSION_FILE = ...
DOWNLOADS_DIR = ...
```

The recommended environment variable names are:

| Setting | Environment variable | Example |
|---|---|---|
| Bot token | `TG_BOT_TOKEN` | `123:ABC...` |
| Owner ID | `OWNER_ID` | `123456789` |
| Extra admins | `ADMIN_IDS` | `123,456` |
| MongoDB | `DATABASE_URL` | `mongodb+srv://...` |
| DB name | `DATABASE_NAME` | `instagram_channel_bot` |
| Monitor | `POST_TIME_SECONDS` | `1800` |
| Recent fetch | `FETCH_LIMIT` | `5` |

## 🔐 Secrets

Do not paste real credentials into GitHub. The values shown in a chat message should also be treated as exposed and rotated when they are real production secrets.

Use `.env` locally or the platform's secret/environment-variable panel on Render/VPS.

---

# 🤖 Commands

The bot is private. Only configured admin numeric IDs can use admin commands.

```text
/start
/admin_help
/ping
/myid

/add_insta @username -1001234567890 [post|reel|both] [label]
/rem_insta @username [channel_id]
/set_insta @username -1001234567890 post|reel|both [label]
/list_insta
/status_insta
/test_insta @username -1001234567890

/allpost @username -1001234567890 [post|reel|both]
/jobs
/cancel_allpost JOB_ID

/pause_monitor
/resume_monitor
```

## ⏱️ Monitor time control

The default value is visible in `config.py` as `POST_TIME_SECONDS = 1800` (30 minutes). You can change it live without restarting the bot:

```text
/set_time 30m
/set_time 45m
/set_time 1h
/set_time 1800s
```

The value is stored in MongoDB and survives restarts. The allowed range is 5 minutes to 7 days.

## 🎛️ POST / REEL control

Default `/add_insta` mode is **POST only**.

Examples:

```text
/add_insta @animepage -1001111111111 post anime
/add_insta @moviepage -1002222222222 post movie
/add_insta @creator -1003333333333 reel reels
/add_insta @creator -1004444444444 both mixed
```

The same Instagram profile can feed different channels with different modes:

```text
/add_insta @creator -1001111111111 post anime
/add_insta @creator -1002222222222 reel reels
```

This is route-based, so Channel 1 and Channel 2 can be completely independent.

---

# 📦 `/allpost`

Uploads the accessible profile history in the background without blocking Telegram commands.

```text
/allpost @sastaotaku -1001111111111 both
```

Only posts matching the selected mode are uploaded:

```text
/allpost @sastaotaku -1001111111111 post
/allpost @sastaotaku -1001111111111 reel
/allpost @sastaotaku -1001111111111 both
```

### Safety design

- Streams posts instead of keeping the complete profile in RAM
- Uses the same single Instagram lock as normal monitoring
- One bulk job runs at a time
- Adds per-item delays and batch pauses
- Stores progress in MongoDB
- Can resume after a restart
- `/cancel_allpost JOB_ID` stops the job safely
- `/jobs` shows progress

It does **not** disable Instagram rate limits.

---

# 🧹 Caption cleaning

Before sending a caption to Telegram, the bot removes:

```text
@username
https://example.com
http://example.com
www.example.com
example.com
example.in/path
```

Then it cleans extra spaces and blank lines.

This changes **caption text only**. Text visually embedded inside an image/video is not removed by the caption sanitizer.

---

# 🍃 MongoDB collections

The bot creates these collections automatically:

```text
feeds        → Instagram → Telegram channel routes
deliveries   → duplicate-safe published markers
jobs         → persistent /allpost jobs
settings     → global monitor ON/OFF state
```

Exact collection names in MongoDB are lowercase:

```text
feeds
deliveries
jobs
settings
```

---

# 🖥️ Local / VPS setup

```bash
cp .env.example .env
nano .env

pip install -r requirements.txt
python bot.py
```

### Docker

```bash
docker compose up -d --build
```

View logs:

```bash
docker compose logs -f instagram-bot
```

---

# ☁️ Render setup

Use a **Background Worker**. The included `render.yaml` and `Dockerfile` are configured for the worker process.

Set these Render variables/secrets:

```text
TG_BOT_TOKEN
OWNER_ID
DATABASE_URL
DATABASE_NAME
```

Optional:

```text
ADMIN_IDS
IG_LOGIN_USERNAME
IG_SESSION_FILE
```

For Render, MongoDB must be hosted somewhere reachable from Render, such as MongoDB Atlas. Do not use `mongodb://localhost` for a remote Render worker.

---

# 📣 Telegram channel setup

1. Add the bot to your channel.
2. Make it an administrator.
3. Give it permission to post messages.
4. Use the channel's numeric ID, commonly looking like:

```text
-1001234567890
```

Then:

```text
/add_insta @sastaotaku -1001234567890 post anime
```

---

# 🧯 Error handling

The project handles common temporary failures without crashing the whole bot:

- Telegram retry-after responses
- Telegram network/server errors
- Instagram connection/temporary errors
- Missing/private Instagram profiles
- Telegram channel permission errors
- Failed individual `/allpost` items
- MongoDB startup connection failure

Repeated monitor errors are stored in MongoDB and admin alerts are throttled so the bot does not spam your DM.

---

# ⚠️ Instagram access

Public-profile monitoring uses Instaloader. Instagram may require login, change endpoints, or temporarily restrict automated access. The bot is designed to reduce request pressure but cannot guarantee that Instagram will never rate-limit or block automated access.

Use only Instagram content that you are permitted to download and repost.
