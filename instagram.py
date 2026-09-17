"""Instagram fetching layer used by the monitor and bulk worker.

Profiles are fetched through their canonical public profile URL
(``https://www.instagram.com/<username>/``) with ``requests`` first. The
previous Instaloader implementation stays available as a fallback whenever the
public page does not expose usable post data or Instagram rate limits us.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

import instaloader
import requests
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

try:  # BeautifulSoup is optional; raw HTML/JSON parsing covers the rest.
    from bs4 import BeautifulSoup
except ModuleNotFoundError:  # pragma: no cover - optional dependency
    BeautifulSoup = None

log = logging.getLogger(__name__)


# =============================================================================
#  Public profile URL fetching
# =============================================================================

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)

PROFILE_URL_TEMPLATE = "https://www.instagram.com/{0}/"

REQUEST_TIMEOUT = 15.0

BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.instagram.com/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

# A logged-out profile page only ever exposes a small recent window.
PROFILE_FETCH_LIMIT = 12
PROFILE_WINDOW_SIZE = 4000
MEDIA_FETCH_LIMIT = 10

_SHORTCODE_RE = re.compile(r'"shortcode"\s*:\s*"(?P<value>[A-Za-z0-9_-]{5,})"')
_CODE_RE = re.compile(
    r'(?<![A-Za-z0-9_])"code"\s*:\s*"(?P<value>[A-Za-z0-9_-]{6,20})"'
)
_POST_URL_RE = re.compile(r"/(?:p|reel|reels|tv)/(?P<value>[A-Za-z0-9_-]{5,})")
_TAKEN_AT_RE = re.compile(
    r'"(?:taken_at_timestamp|taken_at)"\s*:\s*(?P<value>\d{9,})'
)
_MEDIA_TYPE_RE = re.compile(r'"media_type"\s*:\s*(?P<value>\d+)')
_IS_VIDEO_RE = re.compile(r'"is_video"\s*:\s*(?P<value>true|false)')
_PRODUCT_TYPE_RE = re.compile(r'"product_type"\s*:\s*"(?P<value>[A-Za-z_]+)"')
_MEDIA_KEYS_RE = re.compile(
    r'"image_versions2"|"video_versions"|"display_url"|"display_resources"'
    r'|"video_url"|"edge_sidecar_to_children"|"carousel_media"'
)
_CAROUSEL_RE = re.compile(r'"edge_sidecar_to_children"|"carousel_media"')
_DISPLAY_URL_RE = re.compile(
    r'"(?:display_url|thumbnail_src)"\s*:\s*"(?P<value>https:(?:[^"\\]|\\.)*)"'
)
_VIDEO_URL_RE = re.compile(r'"video_url"\s*:\s*"(?P<value>https:(?:[^"\\]|\\.)*)"')
_IMAGE_VERSIONS_URL_RE = re.compile(
    r'"image_versions2"\s*:\s*\{\s*"candidates"\s*:\s*\[\s*\{'
    r'.{0,300}?"url"\s*:\s*"(?P<value>https:(?:[^"\\]|\\.)*)"',
    re.DOTALL,
)
_VIDEO_VERSIONS_URL_RE = re.compile(
    r'"video_versions"\s*:\s*\[\s*\{'
    r'.{0,300}?"url"\s*:\s*"(?P<value>https:(?:[^"\\]|\\.)*)"',
    re.DOTALL,
)
_CAPTION_RE = re.compile(
    r'"edge_media_to_caption"\s*:\s*\{\s*"edges"\s*:\s*\[\s*\{\s*"node"\s*:'
    r'\s*\{\s*"text"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"'
)
_CAPTION_OBJECT_RE = re.compile(
    r'"caption"\s*:\s*\{\s*"text"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"'
)
_CAPTION_STRING_RE = re.compile(r'"caption"\s*:\s*"(?P<value>(?:[^"\\]|\\.)*)"')

_EPOCH_UTC = datetime.min.replace(tzinfo=UTC)


# =============================================================================
# 🧯 Public profile page errors
# =============================================================================


class InstagramHTTPError(RuntimeError):
    """Raised when the public profile page cannot provide usable post data."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.transient = transient


class InstagramRateLimited(InstagramHTTPError):
    """Raised for HTTP 429. This layer never retries or sleeps on it."""

    def __init__(self, message: str, *, status_code: int = 429) -> None:
        super().__init__(message, status_code=status_code, transient=True)


# =============================================================================
# 📦 Post records
# =============================================================================


@dataclass(slots=True)
class PublicPost:
    """Public post data parsed from a profile page fetched over HTTP."""

    shortcode: str
    caption: str = ""
    date_utc: datetime | None = None
    kind: str = "post"
    is_video: bool = False
    is_carousel: bool = False
    media_urls: list[str] = field(default_factory=list)
    thumbnail_url: str | None = None


@dataclass(slots=True)
class DownloadedPost:
    """Media files and metadata produced by a successful Instagram download."""

    shortcode: str
    caption: str
    created_at: str | None
    files: list[Path]


def _json_text(raw: str) -> str:
    """Decode a JSON string body, keeping the text unchanged on failure."""

    try:
        return json.loads('"' + raw + '"')
    except (TypeError, ValueError):
        return raw


def _parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp into an aware UTC datetime."""

    if not isinstance(value, str) or not value.strip():
        return None

    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _shortcode_from_url(url: str) -> str:
    """Return the post shortcode from an Instagram post or reel URL."""

    match = _POST_URL_RE.search(url or "")
    return match.group("value") if match else ""


def _iter_url_values(value: object) -> Iterator[str]:
    """Yield URL strings from a JSON-LD value (string, list or object)."""

    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _iter_url_values(item)
    elif isinstance(value, dict):
        for key in ("url", "contentUrl", "@id"):
            if key in value:
                yield from _iter_url_values(value[key])


def _iter_schema_nodes(node: object) -> Iterator[dict]:
    """Yield schema.org post nodes from a decoded JSON-LD document."""

    if isinstance(node, list):
        for item in node:
            yield from _iter_schema_nodes(item)
        return

    if not isinstance(node, dict):
        return

    if str(node.get("@type", "")).lower() in {
        "imageobject",
        "videoobject",
        "socialmediaposting",
        "article",
        "newsarticle",
    }:
        yield node

    for key in (
        "mainEntity",
        "hasPart",
        "@graph",
        "itemListElement",
        "associatedMedia",
        "sharedContent",
    ):
        if key in node:
            yield from _iter_schema_nodes(node[key])


class _NoWaitRateController(instaloader.RateController):
    """Rate controller that raises instead of sleeping.

    Instaloader normally waits inside :meth:`sleep` (up to eleven minutes) when
    Instagram answers HTTP 429. Failing fast keeps Telegram command handlers and
    monitor cycles responsive; the caller retries later instead.
    """

    def sleep(self, secs: float) -> None:
        raise TooManyRequestsException(
            "Instagram asked us to slow down; request skipped instead of waiting."
        )


class InstagramClient:
    """Public profile page client with an Instaloader fallback."""

    ALLOWED_MEDIA = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov"}

    def __init__(
        self,
        downloads_dir: str,
        *,
        login_username: str = "",
        session_file: str = "",
        use_public_profile_fetch: bool = True,
    ) -> None:
        self.downloads_dir = Path(downloads_dir)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        self.use_public_profile_fetch = use_public_profile_fetch

        # One shared session keeps the normal browser headers and connection
        # pool for every public profile request.
        self.session = requests.Session()
        self.session.headers.update(BROWSER_HEADERS)

        self._login_username = login_username
        self._session_file = session_file
        self._fast_loader: instaloader.Instaloader | None = None

        self.loader = self._build_loader()

    def _build_loader(self, *, fast_fail: bool = False) -> instaloader.Instaloader:
        """Build an Instaloader instance, optionally without any waiting."""

        loader = instaloader.Instaloader(
            dirname_pattern=str(self.downloads_dir / "{target}"),
            filename_pattern="{shortcode}_{date_utc}",
            save_metadata=False,
            download_comments=False,
            download_geotags=False,
            post_metadata_txt_pattern="",
            compress_json=False,
            quiet=True,
            sleep=not fast_fail,
            max_connection_attempts=2 if fast_fail else 3,
            rate_controller=(
                (lambda context: _NoWaitRateController(context)) if fast_fail else None
            ),
        )

        if self._login_username or self._session_file:
            if not self._login_username or not self._session_file:
                raise RuntimeError(
                    "IG_LOGIN_USERNAME and IG_SESSION_FILE must both be set."
                )

            session_path = Path(self._session_file)
            if not session_path.exists():
                raise RuntimeError(
                    f"Instagram session file was not found: {self._session_file}"
                )

            loader.load_session_from_file(
                self._login_username,
                str(session_path),
            )

        return loader

    def _failure_loader(self) -> instaloader.Instaloader:
        """Return the fast-fail Instaloader used for fallback fetches."""

        if self._fast_loader is None:
            self._fast_loader = self._build_loader(fast_fail=True)
        return self._fast_loader

    def latest_posts(self, username: str, limit: int = 5):
        """Return only a small recent window for normal monitoring.

        The canonical public profile URL is tried first. The previous
        Instaloader implementation is used whenever the public page does not
        expose enough post data.
        """

        if self.use_public_profile_fetch:
            try:
                return self.fetch_public_posts(username, limit)
            except InstagramRateLimited as exc:
                log.warning(
                    "Public profile page rate limited for @%s: %s",
                    username,
                    exc,
                )
                return self._instaloader_latest_posts(
                    username,
                    limit,
                    fast_fail=True,
                )
            except InstagramHTTPError as exc:
                log.info("Falling back to Instaloader for @%s: %s", username, exc)

        return self._instaloader_latest_posts(username, limit)

    def iter_posts(self, username: str):
        """Stream posts newest → oldest without storing the whole profile.

        Posts found through the public profile URL are yielded first, then the
        Instaloader implementation continues the stream so ``/allpost`` keeps
        its ordering and resume behaviour. Duplicate shortcodes are skipped.
        """

        yielded: set[str] = set()

        if self.use_public_profile_fetch:
            try:
                public_posts = self.fetch_public_posts(
                    username,
                    PROFILE_FETCH_LIMIT,
                )
            except InstagramRateLimited as exc:
                log.warning(
                    "Public profile page rate limited for @%s: %s",
                    username,
                    exc,
                )
                yield from self._iter_instaloader_posts(username, fast_fail=True)
                return
            except InstagramHTTPError as exc:
                log.info("Falling back to Instaloader for @%s: %s", username, exc)
            else:
                for post in public_posts:
                    yielded.add(post.shortcode)
                    yield post

        for post in self._iter_instaloader_posts(username):
            if post.shortcode in yielded:
                continue
            yield post

    @staticmethod
    def post_kind(post) -> str:
        """Classify Reels as ``reel`` and normal feed items as ``post``."""

        if isinstance(post, PublicPost):
            return "reel" if post.kind == "reel" else "post"

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

        if isinstance(exc, InstagramHTTPError):
            return exc.transient or exc.status_code == 429

        return isinstance(
            exc,
            (
                TooManyRequestsException,
                ConnectionException,
                BadResponseException,
                requests.RequestException,
            ),
        )

    @staticmethod
    def is_access_error(exc: Exception) -> bool:
        """Return True when Instagram access/profile visibility is the problem."""

        if isinstance(exc, InstagramHTTPError) and exc.status_code in {401, 403, 404}:
            return True

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

        if isinstance(post, PublicPost):
            return self._download_public_post(post, username)

        return self._download_instaloader_post(post, username)

    def _download_instaloader_post(self, post, username: str) -> DownloadedPost:
        """Download an Instaloader post (previous behaviour, unchanged)."""

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

    # =========================================================================
    # 🌐 Public profile URL fetching
    # =========================================================================

    @staticmethod
    def profile_url(username: str) -> str:
        """Return the canonical public profile URL for a username or @handle."""

        handle = username.strip()
        if "@" in handle:
            handle = handle.rsplit("@", 1)[-1]
        handle = handle.strip("/")
        if "/" in handle:
            handle = handle.rsplit("/", 1)[-1]
        return PROFILE_URL_TEMPLATE.format(handle)

    def fetch_public_posts(
        self,
        username: str,
        limit: int = PROFILE_FETCH_LIMIT,
    ) -> list[PublicPost]:
        """Fetch and parse posts from the public profile URL.

        Raises :class:`InstagramHTTPError` (or :class:`InstagramRateLimited`)
        when the page cannot provide usable data, so callers can fall back to
        Instaloader.
        """

        html = self._get_profile_page(username)
        posts = self._parse_profile_html(html)

        if not posts:
            raise InstagramHTTPError(
                "Instagram profile page did not expose any readable public post "
                f"for {self.profile_url(username)}."
            )

        return posts[:limit] if limit else posts

    def _get_profile_page(self, username: str) -> str:
        """GET the canonical profile URL once, with a finite timeout."""

        url = self.profile_url(username)

        try:
            response = self.session.get(url, timeout=REQUEST_TIMEOUT)
        except requests.Timeout as exc:
            raise InstagramHTTPError(
                f"Instagram profile request timed out after {REQUEST_TIMEOUT}s: {url}",
                transient=True,
            ) from exc
        except requests.RequestException as exc:
            raise InstagramHTTPError(
                f"Instagram profile request failed: {exc}",
                transient=True,
            ) from exc

        if response.status_code == 429:
            raise InstagramRateLimited(
                f"Instagram rate limited the profile page: {url}"
            )

        if response.status_code != 200:
            raise InstagramHTTPError(
                f"Instagram profile page returned HTTP {response.status_code}: {url}",
                status_code=response.status_code,
                transient=response.status_code >= 500,
            )

        return response.text

    # =========================================================================
    #  Public HTML parsing
    # =========================================================================

    def _parse_profile_html(self, html: str) -> list[PublicPost]:
        """Extract only the post data the public page actually exposes."""

        posts: dict[str, PublicPost] = {}

        if BeautifulSoup is not None:
            soup = self._make_soup(html)
            for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
                raw = tag.string or tag.get_text() or ""
                try:
                    document = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                for node in _iter_schema_nodes(document):
                    post = self._post_from_schema_node(node)
                    if post is not None:
                        posts.setdefault(post.shortcode, post)

        for shortcode, window in self._shortcode_windows(html).items():
            post = posts.get(shortcode)
            if post is None:
                post = PublicPost(shortcode=shortcode)
                posts[shortcode] = post
            self._enrich_from_window(post, window)

        usable = [
            post for post in posts.values() if post.media_urls or post.thumbnail_url
        ]
        usable.sort(key=lambda post: post.date_utc or _EPOCH_UTC, reverse=True)
        return usable

    @staticmethod
    def _make_soup(html: str):
        """Parse HTML with lxml when available, else the stdlib parser."""

        try:
            return BeautifulSoup(html, "lxml")
        except Exception:
            return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _shortcode_windows(html: str) -> dict[str, str]:
        """Map every shortcode in the page to the HTML of its own node.

        Each window ends at the next shortcode/code marker (capped by
        :data:`PROFILE_WINDOW_SIZE`) so that neighbouring posts can never leak
        media URLs or a product type into each other.
        """

        matches: list[tuple[int, str]] = []

        for pattern in (_SHORTCODE_RE, _CODE_RE):
            for match in pattern.finditer(html):
                value = match.group("value")
                if pattern is _CODE_RE and not any(char.isalpha() for char in value):
                    continue
                matches.append((match.start(), value))

        matches.sort()

        windows: dict[str, str] = {}
        for index, (start, shortcode) in enumerate(matches):
            if shortcode in windows:
                continue

            end = start + PROFILE_WINDOW_SIZE
            if index + 1 < len(matches):
                end = min(end, matches[index + 1][0])

            window = html[start:end]
            if _MEDIA_KEYS_RE.search(window):
                windows[shortcode] = window

        return windows

    @staticmethod
    def _enrich_from_window(post: PublicPost, window: str) -> None:
        """Fill missing post fields from the raw HTML window of one shortcode."""

        if _CAROUSEL_RE.search(window):
            post.is_carousel = True

        product_type = _PRODUCT_TYPE_RE.search(window)
        if product_type and product_type.group("value").lower() in {
            "clips",
            "reels",
            "reel",
        }:
            post.kind = "reel"

        media_type = _MEDIA_TYPE_RE.search(window)
        if media_type and media_type.group("value") == "2":
            post.is_video = True

        is_video = _IS_VIDEO_RE.search(window)
        if is_video and is_video.group("value") == "true":
            post.is_video = True

        if post.date_utc is None:
            taken_at = _TAKEN_AT_RE.search(window)
            if taken_at:
                post.date_utc = datetime.fromtimestamp(
                    int(taken_at.group("value")),
                    tz=UTC,
                )

        if not post.caption:
            for pattern in (_CAPTION_RE, _CAPTION_OBJECT_RE, _CAPTION_STRING_RE):
                match = pattern.search(window)
                if match:
                    post.caption = _json_text(match.group("value"))
                    break

        if post.media_urls:
            return

        if post.is_video:
            video_url = _VIDEO_URL_RE.search(
                window
            ) or _VIDEO_VERSIONS_URL_RE.search(window)
            if video_url:
                post.media_urls = [_json_text(video_url.group("value"))]
                return

        image_url = _IMAGE_VERSIONS_URL_RE.search(
            window
        ) or _DISPLAY_URL_RE.search(window)
        if image_url:
            post.media_urls = [_json_text(image_url.group("value"))]
            if post.thumbnail_url is None:
                post.thumbnail_url = post.media_urls[0]

    @staticmethod
    def _post_from_schema_node(node: dict) -> PublicPost | None:
        """Build a PublicPost from one schema.org (JSON-LD) node."""

        url = str(node.get("url") or node.get("mainEntityOfPage") or "")
        shortcode = _shortcode_from_url(url)
        if not shortcode:
            return None

        caption = ""
        for key in ("caption", "description", "name"):
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                caption = value.strip()
                break

        date_utc = _parse_iso_datetime(
            node.get("uploadDate")
            or node.get("dateCreated")
            or node.get("dateModified")
        )

        media_urls: list[str] = []
        content_url = node.get("contentUrl")
        if isinstance(content_url, str) and content_url.startswith("http"):
            media_urls.append(content_url)

        thumbnail_url: str | None = None
        for candidate in _iter_url_values(node.get("image")):
            if candidate.startswith("http"):
                thumbnail_url = candidate
                break

        if not media_urls and thumbnail_url:
            media_urls.append(thumbnail_url)

        if not media_urls:
            return None

        return PublicPost(
            shortcode=shortcode,
            caption=caption,
            date_utc=date_utc,
            kind="reel" if "/reel" in url else "post",
            is_video=str(node.get("@type", "")).lower() == "videoobject",
            media_urls=media_urls,
            thumbnail_url=thumbnail_url,
        )

    def _download_public_post(self, post: PublicPost, username: str) -> DownloadedPost:
        """Download a public-page post, falling back to Instaloader if needed."""

        if post.is_carousel or not post.media_urls:
            # Carousels need every child media item, which Instaloader exposes
            # reliably while the public page does not.
            return self._download_with_instaloader_shortcode(post, username)

        target_dir = self.downloads_dir / username / post.shortcode
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        target_dir.mkdir(parents=True, exist_ok=True)

        try:
            files = self._download_media_urls(post, target_dir)
        except InstagramRateLimited:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
        except Exception as exc:
            shutil.rmtree(target_dir, ignore_errors=True)
            log.warning(
                "Public media download failed for %s (%s); using Instaloader.",
                post.shortcode,
                exc,
            )
            files = []

        if not files:
            shutil.rmtree(target_dir, ignore_errors=True)
            return self._download_with_instaloader_shortcode(post, username)

        return DownloadedPost(
            shortcode=post.shortcode,
            caption=post.caption or "",
            created_at=post.date_utc.isoformat() if post.date_utc else None,
            files=files,
        )

    def _download_media_urls(self, post: PublicPost, target_dir: Path) -> list[Path]:
        """Download the public media URLs with a finite timeout."""

        files: list[Path] = []

        for index, url in enumerate(post.media_urls[:MEDIA_FETCH_LIMIT], start=1):
            try:
                response = self.session.get(
                    url,
                    timeout=REQUEST_TIMEOUT,
                    stream=True,
                )
            except requests.RequestException as exc:
                raise InstagramHTTPError(
                    f"Media download failed: {exc}",
                    transient=True,
                ) from exc

            with response:
                if response.status_code == 429:
                    raise InstagramRateLimited(
                        "Instagram rate limited a public media download."
                    )

                if response.status_code != 200:
                    raise InstagramHTTPError(
                        f"Media download returned HTTP {response.status_code}.",
                        status_code=response.status_code,
                        transient=response.status_code >= 500,
                    )

                extension = self._media_extension(response, url, post.is_video)
                if extension not in self.ALLOWED_MEDIA:
                    continue

                path = target_dir / f"{post.shortcode}_{index:02d}{extension}"

                with path.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            handle.write(chunk)

            if path.stat().st_size == 0:
                path.unlink(missing_ok=True)
                continue

            files.append(path)

        return sorted(files, key=lambda item: item.name)

    @staticmethod
    def _media_extension(response, url: str, is_video: bool) -> str:
        """Pick a supported file extension from the response or the URL."""

        content_type = (
            (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        )
        mapping = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
        }
        if content_type in mapping:
            return mapping[content_type]

        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix in InstagramClient.ALLOWED_MEDIA:
            return suffix
        return ".mp4" if is_video else ".jpg"

    def _download_with_instaloader_shortcode(
        self,
        post: PublicPost,
        username: str,
    ) -> DownloadedPost:
        """Resolve one shortcode through Instaloader and download it."""

        try:
            instaloader_post = instaloader.Post.from_shortcode(
                self.loader.context,
                post.shortcode,
            )
        except Exception as exc:
            raise InstagramHTTPError(
                f"Instagram post {post.shortcode} could not be loaded through "
                f"Instaloader: {exc}",
                transient=isinstance(
                    exc,
                    (TooManyRequestsException, ConnectionException),
                ),
            ) from exc

        downloaded = self._download_instaloader_post(instaloader_post, username)

        return DownloadedPost(
            shortcode=post.shortcode,
            caption=downloaded.caption or post.caption or "",
            created_at=(
                downloaded.created_at
                or (post.date_utc.isoformat() if post.date_utc else None)
            ),
            files=downloaded.files,
        )

    # =========================================================================
    # 🧯 Instaloader fallback
    # =========================================================================

    def _instaloader_latest_posts(
        self,
        username: str,
        limit: int = 5,
        *,
        fast_fail: bool = False,
    ):
        """Return the small recent window straight from Instaloader."""

        loader = self._failure_loader() if fast_fail else self.loader
        profile = instaloader.Profile.from_username(loader.context, username)

        posts = []
        for post in profile.get_posts():
            posts.append(post)
            if len(posts) >= limit:
                break
        return posts

    def _iter_instaloader_posts(self, username: str, *, fast_fail: bool = False):
        """Stream Instaloader posts newest → oldest."""

        loader = self._failure_loader() if fast_fail else self.loader
        profile = instaloader.Profile.from_username(loader.context, username)
        yield from profile.get_posts()

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
