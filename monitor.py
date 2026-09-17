"""24/7 monitor and persistent /allpost background worker."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from aiogram import Bot

from database import Database
from instagram import InstagramClient
from uploader import send_post

log = logging.getLogger(__name__)


class Monitor:
    """Coordinates lightweight monitoring and one-at-a-time bulk jobs."""

    def __init__(
        self,
        bot: Bot,
        db: Database,
        ig: InstagramClient,
        *,
        interval: int,
        fetch_limit: int,
        profile_delay: float,
        allpost_item_delay: float,
        allpost_batch_size: int,
        allpost_batch_pause: float,
        telegram_upload_delay: float,
        max_retries: int,
        error_alert_cooldown: int,
        admin_ids: frozenset[int],
    ) -> None:
        self.bot = bot
        self.db = db
        self.ig = ig

        self.interval = interval
        self.fetch_limit = fetch_limit
        self.profile_delay = profile_delay
        self.allpost_item_delay = allpost_item_delay
        self.allpost_batch_size = allpost_batch_size
        self.allpost_batch_pause = allpost_batch_pause
        self.telegram_upload_delay = telegram_upload_delay
        self.max_retries = max_retries
        self.error_alert_cooldown = error_alert_cooldown
        self.admin_ids = admin_ids

        # Only one Instagram operation runs at a time. This keeps normal
        # monitoring and /allpost from hitting Instagram in parallel.
        self._ig_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    async def run(self) -> None:
        """Start both background loops until the application shuts down."""

        recovered = self.db.recover_running_jobs()
        if recovered:
            log.info("Recovered %s interrupted allpost job(s)", recovered)

        monitor_task = asyncio.create_task(
            self._monitor_loop(),
            name="instagram-monitor-loop",
        )
        job_task = asyncio.create_task(
            self._job_loop(),
            name="allpost-job-loop",
        )

        try:
            await self._stop.wait()
        finally:
            for task in (monitor_task, job_task):
                task.cancel()
            await asyncio.gather(
                monitor_task,
                job_task,
                return_exceptions=True,
            )

    async def stop(self) -> None:
        """Tell the background loops to stop."""

        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Wake the monitor so a changed /set_time applies immediately."""

        self._wake.set()

    # =========================================================================
    # 📡 Normal 30-minute monitoring
    # =========================================================================

    async def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            if self.db.is_monitor_enabled():
                try:
                    await self.scan_all()
                except Exception:
                    log.exception("Monitor cycle failed")

            # The interval is stored in MongoDB so /set_time applies without
            # restarting the bot. The wake event also applies it immediately.
            current_interval = self.db.get_monitor_interval(self.interval)
            self._wake.clear()

            stop_task = asyncio.create_task(self._stop.wait())
            wake_task = asyncio.create_task(self._wake.wait())
            try:
                done, pending = await asyncio.wait(
                    {stop_task, wake_task},
                    timeout=current_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (stop_task, wake_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(stop_task, wake_task, return_exceptions=True)

            if self._stop.is_set():
                return

    async def scan_all(self) -> None:
        """Fetch each unique Instagram profile once, then route to channels."""

        feeds = self.db.due_feeds()
        grouped: dict[str, list[dict[str, Any]]] = {}

        for feed in feeds:
            grouped.setdefault(feed["username"], []).append(feed)

        for index, (username, routes) in enumerate(grouped.items()):
            if self._stop.is_set():
                return

            try:
                async with self._ig_lock:
                    posts = await asyncio.to_thread(
                        self.ig.latest_posts,
                        username,
                        self.fetch_limit,
                    )

                if not posts:
                    raise RuntimeError("No readable Instagram posts returned")

                newest = posts[0]

                for route in routes:
                    await self._process_route(
                        username,
                        route,
                        posts,
                        newest,
                    )

            except Exception as exc:
                await self._handle_feed_error(username, routes, exc)

            if self.profile_delay and index < len(grouped) - 1:
                await asyncio.sleep(
                    self.profile_delay
                    + random.uniform(
                        0,
                        min(1.5, self.profile_delay),
                    )
                )

    async def _process_route(
        self,
        username: str,
        route: dict[str, Any],
        posts: list[Any],
        newest: Any,
    ) -> None:
        chat_id = int(route["chat_id"])
        mode = route.get("mode", "post")
        last_shortcode = route.get("last_shortcode")

        # First add: mark the current newest item as seen. This prevents a
        # freshly-created feed from dumping old content into the channel.
        if not last_shortcode:
            self.db.mark_initial(
                username,
                chat_id,
                newest.shortcode,
                newest.date_utc.isoformat() if newest.date_utc else None,
            )
            return

        new_posts = self._new_posts(posts, last_shortcode)

        # The regular monitor only handles the recent window. It does not
        # automatically backfill an entire profile if the marker is too old.
        for post in new_posts:
            kind = self.ig.post_kind(post)
            if not self._matches_mode(kind, mode):
                continue

            if self.db.delivery_sent(
                chat_id,
                username,
                post.shortcode,
            ):
                self.db.mark_success(
                    username,
                    chat_id,
                    post.shortcode,
                    post.date_utc.isoformat() if post.date_utc else None,
                )
                continue

            await self._download_and_send(
                username=username,
                chat_id=chat_id,
                post=post,
                kind=kind,
            )

        # Always move the feed marker to the newest observed item.
        self.db.mark_success(
            username,
            chat_id,
            newest.shortcode,
            newest.date_utc.isoformat() if newest.date_utc else None,
        )

    async def _download_and_send(
        self,
        *,
        username: str,
        chat_id: int,
        post: Any,
        kind: str,
    ) -> None:
        async with self._ig_lock:
            downloaded = await asyncio.to_thread(
                self.ig.download_post,
                post,
                username,
            )

        try:
            await send_post(
                self.bot,
                chat_id,
                downloaded.files,
                downloaded.caption,
                source_kind=kind,
                upload_delay=self.telegram_upload_delay,
                max_retries=self.max_retries,
            )

            self.db.mark_delivery_sent(
                chat_id,
                username,
                post.shortcode,
                kind,
            )
            log.info(
                "Published @%s/%s (%s) → %s",
                username,
                post.shortcode,
                kind,
                chat_id,
            )
        finally:
            await asyncio.to_thread(
                self.ig.cleanup_files,
                downloaded.files,
            )

    @staticmethod
    def _matches_mode(post_kind: str, mode: str) -> bool:
        return mode == "both" or mode == post_kind

    @staticmethod
    def _new_posts(
        posts: list[Any],
        last_shortcode: str | None,
    ) -> list[Any]:
        """Return new items in oldest → newest order."""

        if not last_shortcode:
            return []

        oldest_first = list(reversed(posts))
        for index, post in enumerate(oldest_first):
            if post.shortcode == last_shortcode:
                return oldest_first[index + 1 :]

        # Marker was outside the small fetch window. Do not create a backlog.
        return [oldest_first[-1]] if oldest_first else []

    # =========================================================================
    # 📦 Persistent /allpost worker
    # =========================================================================

    async def _job_loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.db.claim_next_job()
                if job:
                    await self._process_allpost_job(job)
                    continue

                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=2,
                )
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Allpost worker loop failed")
                await asyncio.sleep(5)

    async def _process_allpost_job(self, job: dict[str, Any]) -> None:
        job_id = job["_id"]
        username = job["username"]
        chat_id = int(job["chat_id"])
        mode = job.get("mode", "both")
        requested_by = int(job["requested_by"])
        resume_shortcode = job.get("last_shortcode")

        processed = int(job.get("processed", 0))
        failed = int(job.get("failed", 0))
        sent_since_pause = 0
        marker_seen = resume_shortcode is None

        try:
            await self._notify_user(
                requested_by,
                "🚀 <b>/allpost started</b>\n"
                f"Profile: @{username}\n"
                f"Channel: <code>{chat_id}</code>\n"
                f"Mode: <b>{mode.upper()}</b>\n"
                f"Job: <code>{job_id}</code>",
            )

            # Stream newest → oldest. We never load the entire profile into RAM.
            post_iterator = self.ig.iter_posts(username)

            while True:
                if self.db.is_job_cancelled(job_id):
                    await self._notify_user(
                        requested_by,
                        f"🛑 Job <code>{job_id}</code> cancelled.",
                    )
                    return

                post = await self._next_post(post_iterator)
                if post is None:
                    break

                if not marker_seen:
                    if post.shortcode == resume_shortcode:
                        marker_seen = True
                    continue

                kind = self.ig.post_kind(post)
                if not self._matches_mode(kind, mode):
                    continue

                if self.db.delivery_sent(
                    chat_id,
                    username,
                    post.shortcode,
                ):
                    self.db.update_job_progress(
                        job_id,
                        last_shortcode=post.shortcode,
                    )
                    continue

                try:
                    await self._download_and_send(
                        username=username,
                        chat_id=chat_id,
                        post=post,
                        kind=kind,
                    )

                    processed += 1
                    sent_since_pause += 1
                    self.db.update_job_progress(
                        job_id,
                        processed_inc=1,
                        last_shortcode=post.shortcode,
                        error=None,
                    )

                    if processed % 10 == 0:
                        await self._notify_user(
                            requested_by,
                            "📦 <b>/allpost progress</b>\n"
                            f"Job: <code>{job_id}</code>\n"
                            f"Uploaded: {processed}\n"
                            f"Failed: {failed}\n"
                            f"Last: <code>{post.shortcode}</code>",
                        )

                    if sent_since_pause >= self.allpost_batch_size:
                        sent_since_pause = 0
                        await asyncio.sleep(self.allpost_batch_pause)
                    elif self.allpost_item_delay:
                        await asyncio.sleep(self.allpost_item_delay)

                except Exception as exc:
                    failed += 1
                    error = f"{type(exc).__name__}: {exc}"
                    self.db.update_job_progress(
                        job_id,
                        failed_inc=1,
                        last_shortcode=post.shortcode,
                        error=error,
                    )
                    log.exception(
                        "Allpost item failed: @%s/%s",
                        username,
                        post.shortcode,
                    )
                    await self._notify_user(
                        requested_by,
                        "⚠️ <b>/allpost skipped one item</b>\n"
                        f"Job: <code>{job_id}</code>\n"
                        f"Post: <code>{post.shortcode}</code>\n"
                        f"Error: <code>{error[:600]}</code>",
                    )

                    if self.allpost_item_delay:
                        await asyncio.sleep(self.allpost_item_delay)

            if not marker_seen and resume_shortcode is not None:
                raise RuntimeError(
                    "Saved resume marker was not found. "
                    "Job stopped to avoid reposting the whole profile."
                )

            self.db.finish_job(job_id, "completed")
            await self._notify_user(
                requested_by,
                "✅ <b>/allpost completed</b>\n"
                f"Job: <code>{job_id}</code>\n"
                f"Uploaded: {processed}\n"
                f"Failed: {failed}",
            )

        except asyncio.CancelledError:
            self.db.finish_job(
                job_id,
                "queued",
                "Worker stopped; job will resume after restart.",
            )
            raise

        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            log.exception("Allpost job failed: %s", job_id)

            retries = int(job.get("retries", 0))
            if self.ig.is_transient_error(exc) and retries < self.max_retries:
                self.db.requeue_job(job_id, error)
                backoff = min(300, 30 * (2**retries))
                await self._notify_user(
                    requested_by,
                    "⏳ <b>/allpost temporary error</b>\n"
                    f"Job: <code>{job_id}</code>\n"
                    f"Retrying in <b>{backoff}s</b>\n"
                    f"<code>{error[:600]}</code>",
                )
                await asyncio.sleep(backoff)
                return

            self.db.finish_job(job_id, "failed", error)
            await self._notify_user(
                requested_by,
                "❌ <b>/allpost failed</b>\n"
                f"Job: <code>{job_id}</code>\n"
                f"<code>{error[:1200]}</code>",
            )

    async def _next_post(self, iterator):
        """Read one blocking Instaloader iterator step without blocking asyncio."""

        def get_next():
            return next(iterator, None)

        async with self._ig_lock:
            return await asyncio.to_thread(get_next)

    async def _notify_user(self, user_id: int, text: str) -> None:
        try:
            await self.bot.send_message(
                user_id,
                text,
                parse_mode="HTML",
            )
        except Exception:
            log.exception("Could not notify user %s", user_id)

    # =========================================================================
    # 🧯 Error alerts
    # =========================================================================

    async def _handle_feed_error(
        self,
        username: str,
        routes: list[dict[str, Any]],
        exc: Exception,
    ) -> None:
        error = f"{type(exc).__name__}: {exc}"
        log.exception("Instagram monitor failed for @%s", username)

        for route in routes:
            chat_id = int(route["chat_id"])
            self.db.mark_error(username, chat_id, error)

            if self.db.should_alert_error(
                username,
                chat_id,
                error,
                self.error_alert_cooldown,
            ):
                self.db.mark_error_alerted(username, chat_id, error)
                await self._notify_admins(
                    "⚠️ <b>Instagram monitor error</b>\n"
                    f"Profile: @{username}\n"
                    f"Channel: <code>{chat_id}</code>\n"
                    f"<code>{error[:1200]}</code>",
                )

    async def _notify_admins(self, text: str) -> None:
        for admin_id in self.admin_ids:
            await self._notify_user(admin_id, text)

    async def publish_latest(self, username: str, chat_id: int) -> str:
        """Manually download and upload the latest readable item."""

        async with self._ig_lock:
            posts = await asyncio.to_thread(
                self.ig.latest_posts,
                username,
                1,
            )

        if not posts:
            raise RuntimeError("No Instagram post found")

        post = posts[0]
        await self._download_and_send(
            username=username,
            chat_id=chat_id,
            post=post,
            kind=self.ig.post_kind(post),
        )
        return post.shortcode
