"""Small Instaloader wrapper used by the monitor and bulk worker."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import instaloader
from instaloader.exceptions import (
    BadResponseException,
    ConnectionException,
    LoginRequiredException,
    PrivateProfileNotFollowedException,
    ProfileNotExistsException,
    QueryReturnedForbiddenException,
    QueryReturnedNotFoundException,
    TooManyRequestsException,
)


@dataclass(slots=True)
class DownloadedPost:
    """Media files and metadata produced by a successful Instagram download."""

    shortcode: str
    caption: str
    created_at: str | None
    files: list[Path]


class InstagramClient:
    """Reusable Instaloader client with lightweight download settings."""

    ALLOWED_MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}

    def __init__(
        self,
        downloads_dir: str,
        *,
        login_username: str = "",
        session_file: str = "",
    ) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.loader = instaloader.Instaloader(
            dirname_pattern=str(self.downloads_dir / "{target}"),
            filename_pattern="{shortcode}_{date_utc}",
            save_metadata=False,
            download_comments=False,
            download_geotags=False,
            post_metadata_txt_pattern="",
            compress_json=False,
            quiet=True,
        )

        if login_username or session_file:
            if not login_username or not session_file:
                raise RuntimeError(
                    "IG_LOGIN_USERNAME and IG_SESSION_FILE must both be set."
                )

            session_path = Path(session_file)
            if not session_path.exists():
                raise RuntimeError(
                    f"Instagram session file was not found: {session_file}"
                )

            self.loader.load_session_from_file(
                login_username,
                str(session_path),
            )

    def latest_posts(self, username: str, limit: int = 5):
        """Return only a small recent window for normal monitoring."""

        profile = instaloader.Profile.from_username(
            self.loader.context,
            username,
        )

        posts = []
        for post in profile.get_posts():
            posts.append(post)
            if len(posts) >= limit:
                break
        return posts

    def iter_posts(self, username: str):
        """Stream posts newest → oldest without storing the whole profile."""

        profile = instaloader.Profile.from_username(
            self.loader.context,
            username,
        )
        yield from profile.get_posts()

    @staticmethod
    def post_kind(post) -> str:
        """Classify Reels as ``reel`` and normal feed items as ``post``."""

        try:
            node = getattr(post, "_node", {}) or {}
            product_type = str(node.get("product_type") or "").lower()
            if product_type in {"clips", "reels", "reel"}:
                return "reel"
        except Exception:
            pass

        return "post"

    @staticmethod
    def is_transient_error(exc: Exception) -> bool:
        """Return True for failures worth retrying later."""

        return isinstance(
            exc,
            (
                TooManyRequestsException,
                ConnectionException,
                BadResponseException,
            ),
        )

    @staticmethod
    def is_access_error(exc: Exception) -> bool:
        """Return True when Instagram access/profile visibility is the problem."""

        return isinstance(
            exc,
            (
                LoginRequiredException,
                PrivateProfileNotFollowedException,
                ProfileNotExistsException,
                QueryReturnedForbiddenException,
                QueryReturnedNotFoundException,
            ),
        )

    def download_post(self, post, username: str) -> DownloadedPost:
        """Download one post into a temporary folder and return its media files."""

        target_dir = self.downloads_dir / username / post.shortcode
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            self.loader.download_post(post, target=str(target_dir))

            files = sorted(
                (
                    path
                    for path in target_dir.rglob("*")
                    if path.is_file() and path.suffix.lower() in self.ALLOWED_MEDIA
                ),
                key=lambda path: path.name,
            )

            if not files:
                raise RuntimeError(
                    "Instagram returned no supported media files for this post."
                )

            created_at = None
            try:
                created_at = (
                    post.date_utc.isoformat() if post.date_utc else None
                )
            except Exception:
                pass

            return DownloadedPost(
                shortcode=post.shortcode,
                caption=post.caption or "",
                created_at=created_at,
                files=files,
            )
        except Exception:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise

    @staticmethod
    def cleanup_files(files: list[Path]) -> None:
        """Delete temporary post folders after successful/failed upload."""

        if not files:
            return

        directories = {path.parent for path in files}
        for directory in directories:
            try:
                if directory.exists():
                    shutil.rmtree(directory, ignore_errors=True)
            except OSError:
                pass
