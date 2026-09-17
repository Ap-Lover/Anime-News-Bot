"""MongoDB storage for feed routes, deliveries, settings and bulk jobs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument


def utc_now() -> datetime:
    """Return an explicit UTC timestamp."""

    return datetime.now(UTC)


class Database:
    """Simple MongoDB repository used by the bot."""

    VALID_MODES = {"post", "reel", "both"}
    VALID_JOB_STATUSES = {
        "queued",
        "running",
        "completed",
        "failed",
        "cancelled",
    }

    def __init__(self, uri: str, database_name: str) -> None:
        self.client = MongoClient(
            uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=15000,
            maxPoolSize=8,
            minPoolSize=0,
            retryWrites=True,
        )

        # Fail quickly at startup instead of discovering a broken database later.
        self.client.admin.command("ping")

        self.db = self.client[database_name]
        self.feeds = self.db["feeds"]
        self.deliveries = self.db["deliveries"]
        self.jobs = self.db["jobs"]
        self.settings = self.db["settings"]

        self._create_indexes()

    def _create_indexes(self) -> None:
        """Create only the indexes needed by the application."""

        self.feeds.create_index(
            [("username", ASCENDING), ("chat_id", ASCENDING)],
            unique=True,
            name="username_chat_unique",
        )
        self.deliveries.create_index(
            [
                ("chat_id", ASCENDING),
                ("username", ASCENDING),
                ("shortcode", ASCENDING),
            ],
            unique=True,
            name="delivery_unique",
        )
        self.jobs.create_index(
            [("status", ASCENDING), ("created_at", ASCENDING)],
            name="job_queue",
        )
        self.jobs.create_index(
            [("created_at", DESCENDING)],
            name="job_created_desc",
        )

    def close(self) -> None:
        """Close the MongoDB connection pool."""

        self.client.close()

    # =========================================================================
    # 📡 Feed routes
    # =========================================================================

    def add_feed(
        self,
        username: str,
        chat_id: int,
        *,
        mode: str = "post",
        label: str = "",
    ) -> bool:
        if mode not in self.VALID_MODES:
            raise ValueError("Mode must be post, reel or both")

        now = utc_now()
        result = self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {
                "$setOnInsert": {
                    "username": username,
                    "chat_id": chat_id,
                    "mode": mode,
                    "label": label[:80],
                    "last_shortcode": None,
                    "last_post_date": None,
                    "last_check": None,
                    "last_error": None,
                    "last_error_alert": None,
                    "last_alert_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
            },
            upsert=True,
        )
        return result.upserted_id is not None

    def remove_feed(
        self,
        username: str,
        chat_id: int | None = None,
    ) -> int:
        query: dict[str, Any] = {"username": username}
        if chat_id is not None:
            query["chat_id"] = chat_id
        return self.feeds.delete_many(query).deleted_count

    def list_feeds(self) -> list[dict[str, Any]]:
        return list(
            self.feeds.find({}).sort(
                [("username", ASCENDING), ("chat_id", ASCENDING)]
            )
        )

    def due_feeds(self) -> list[dict[str, Any]]:
        """Return all routes; the current worker runs them once per cycle."""

        return self.list_feeds()

    def get_feed(
        self,
        username: str,
        chat_id: int,
    ) -> dict[str, Any] | None:
        return self.feeds.find_one({"username": username, "chat_id": chat_id})

    def set_mode(self, username: str, chat_id: int, mode: str) -> bool:
        if mode not in self.VALID_MODES:
            raise ValueError("Mode must be post, reel or both")

        result = self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {"$set": {"mode": mode, "updated_at": utc_now()}},
        )
        return result.matched_count == 1

    def set_label(self, username: str, chat_id: int, label: str) -> bool:
        result = self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {"$set": {"label": label[:80], "updated_at": utc_now()}},
        )
        return result.matched_count == 1

    def mark_initial(
        self,
        username: str,
        chat_id: int,
        shortcode: str,
        post_date: str | None,
    ) -> None:
        self._mark(username, chat_id, shortcode, post_date)

    def mark_success(
        self,
        username: str,
        chat_id: int,
        shortcode: str,
        post_date: str | None,
    ) -> None:
        self._mark(username, chat_id, shortcode, post_date)

    def _mark(
        self,
        username: str,
        chat_id: int,
        shortcode: str,
        post_date: str | None,
    ) -> None:
        now = utc_now()
        self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {
                "$set": {
                    "last_shortcode": shortcode,
                    "last_post_date": post_date,
                    "last_check": now,
                    "last_error": None,
                    "updated_at": now,
                }
            },
        )

    def mark_error(self, username: str, chat_id: int, error: str) -> None:
        self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {
                "$set": {
                    "last_check": utc_now(),
                    "last_error": error[:2000],
                    "updated_at": utc_now(),
                }
            },
        )

    def should_alert_error(
        self,
        username: str,
        chat_id: int,
        error: str,
        cooldown_seconds: int,
    ) -> bool:
        feed = self.get_feed(username, chat_id)
        if not feed:
            return False

        now = utc_now()
        normalized_error = error[:2000]
        previous_error = feed.get("last_error_alert")
        last_alert = feed.get("last_alert_at")

        if previous_error != normalized_error:
            return True

        return not last_alert or (
            now - last_alert
        ).total_seconds() >= cooldown_seconds

    def mark_error_alerted(
        self,
        username: str,
        chat_id: int,
        error: str,
    ) -> None:
        now = utc_now()
        self.feeds.update_one(
            {"username": username, "chat_id": chat_id},
            {
                "$set": {
                    "last_error_alert": error[:2000],
                    "last_alert_at": now,
                    "updated_at": now,
                }
            },
        )

    # =========================================================================
    # ✅ Duplicate-safe deliveries
    # =========================================================================

    def delivery_sent(
        self,
        chat_id: int,
        username: str,
        shortcode: str,
    ) -> bool:
        return (
            self.deliveries.find_one(
                {
                    "chat_id": chat_id,
                    "username": username,
                    "shortcode": shortcode,
                }
            )
            is not None
        )

    def mark_delivery_sent(
        self,
        chat_id: int,
        username: str,
        shortcode: str,
        kind: str,
    ) -> None:
        now = utc_now()
        self.deliveries.update_one(
            {
                "chat_id": chat_id,
                "username": username,
                "shortcode": shortcode,
            },
            {
                "$set": {"kind": kind, "sent_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )

    # =========================================================================
    # ⏱️ Monitor timing
    # =========================================================================

    def set_monitor_interval(self, seconds: int) -> None:
        """Store the live monitor interval in MongoDB."""

        if seconds < 300:
            raise ValueError("Monitor interval cannot be below 5 minutes")

        self.settings.update_one(
            {"_id": "global"},
            {
                "$set": {
                    "monitor_interval_seconds": int(seconds),
                    "updated_at": utc_now(),
                }
            },
            upsert=True,
        )

    def get_monitor_interval(self, default_seconds: int) -> int:
        """Read live monitor interval, falling back to config.py default."""

        document = self.settings.find_one(
            {"_id": "global"},
            {"monitor_interval_seconds": 1},
        )
        value = document.get("monitor_interval_seconds") if document else None
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = int(default_seconds)
        return max(300, value)

    # =========================================================================
    # ⏯ Monitor state
    # =========================================================================

    def set_monitor_enabled(self, enabled: bool) -> None:
        self.settings.update_one(
            {"_id": "global"},
            {
                "$set": {
                    "monitor_enabled": bool(enabled),
                    "updated_at": utc_now(),
                }
            },
            upsert=True,
        )

    def is_monitor_enabled(self) -> bool:
        document = self.settings.find_one({"_id": "global"})
        if not document or "monitor_enabled" not in document:
            return True
        return bool(document["monitor_enabled"])

    # =========================================================================
    # 📦 /allpost jobs
    # =========================================================================

    def recover_running_jobs(self) -> int:
        result = self.jobs.update_many(
            {"status": "running"},
            {
                "$set": {
                    "status": "queued",
                    "recovered_at": utc_now(),
                    "updated_at": utc_now(),
                }
            },
        )
        return result.modified_count

    def create_allpost_job(
        self,
        username: str,
        chat_id: int,
        requested_by: int,
        *,
        mode: str = "both",
        label: str = "",
    ) -> str:
        if mode not in self.VALID_MODES:
            raise ValueError("Mode must be post, reel or both")

        now = utc_now()
        result = self.jobs.insert_one(
            {
                "type": "allpost",
                "username": username,
                "chat_id": chat_id,
                "requested_by": requested_by,
                "mode": mode,
                "label": label[:80],
                "status": "queued",
                "processed": 0,
                "failed": 0,
                "last_shortcode": None,
                "last_error": None,
                "retries": 0,
                "created_at": now,
                "updated_at": now,
                "finished_at": None,
            }
        )
        return str(result.inserted_id)

    def _as_object_id(self, job_id: Any) -> ObjectId:
        if isinstance(job_id, ObjectId):
            return job_id
        return ObjectId(str(job_id))

    def claim_next_job(self) -> dict[str, Any] | None:
        return self.jobs.find_one_and_update(
            {"status": "queued", "type": "allpost"},
            {
                "$set": {
                    "status": "running",
                    "started_at": utc_now(),
                    "updated_at": utc_now(),
                }
            },
            sort=[("created_at", ASCENDING)],
            return_document=ReturnDocument.AFTER,
        )

    def update_job_progress(
        self,
        job_id: Any,
        *,
        processed_inc: int = 0,
        failed_inc: int = 0,
        last_shortcode: str | None = None,
        error: str | None = None,
    ) -> None:
        update: dict[str, Any] = {"$set": {"updated_at": utc_now()}}
        increments: dict[str, int] = {}

        if processed_inc:
            increments["processed"] = processed_inc
        if failed_inc:
            increments["failed"] = failed_inc
        if increments:
            update["$inc"] = increments

        if last_shortcode is not None:
            update["$set"]["last_shortcode"] = last_shortcode
        if error is not None:
            update["$set"]["last_error"] = error[:2000]

        self.jobs.update_one({"_id": self._as_object_id(job_id)}, update)

    def finish_job(
        self,
        job_id: Any,
        status: str,
        error: str | None = None,
    ) -> None:
        if status not in self.VALID_JOB_STATUSES:
            raise ValueError("Invalid job status")

        values: dict[str, Any] = {
            "status": status,
            "finished_at": utc_now(),
            "updated_at": utc_now(),
        }
        if error:
            values["last_error"] = error[:2000]

        self.jobs.update_one(
            {"_id": self._as_object_id(job_id)},
            {"$set": values},
        )

    def requeue_job(self, job_id: Any, error: str | None = None) -> None:
        values: dict[str, Any] = {
            "status": "queued",
            "updated_at": utc_now(),
        }
        if error:
            values["last_error"] = error[:2000]

        self.jobs.update_one(
            {"_id": self._as_object_id(job_id)},
            {"$set": values, "$inc": {"retries": 1}},
        )

    def cancel_job(self, job_id_text: str) -> bool:
        try:
            job_id = self._as_object_id(job_id_text)
        except Exception:
            return False

        result = self.jobs.update_one(
            {
                "_id": job_id,
                "status": {"$in": ["queued", "running"]},
            },
            {
                "$set": {
                    "status": "cancelled",
                    "finished_at": utc_now(),
                    "updated_at": utc_now(),
                }
            },
        )
        return result.modified_count == 1

    def is_job_cancelled(self, job_id: Any) -> bool:
        document = self.jobs.find_one(
            {"_id": self._as_object_id(job_id)},
            {"status": 1},
        )
        return bool(document and document.get("status") == "cancelled")

    def list_jobs(self, limit: int = 15) -> list[dict[str, Any]]:
        return list(
            self.jobs.find({})
            .sort("created_at", DESCENDING)
            .limit(limit)
        )
