"""Offline unit tests for the Instagram API v1 feed fetcher.

All HTTP requests are mocked — no real Instagram credentials or network
access is needed.
"""

from __future__ import annotations

import json
import sys
import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Mock environment so config.py does not raise on import
# ---------------------------------------------------------------------------
_ENV_DEFAULTS = {
    "TG_BOT_TOKEN": "fake:token",
    "OWNER_ID": "123456789",
    "DATABASE_URL": "mongodb://localhost/test",
}

import os

for key, value in _ENV_DEFAULTS.items():
    os.environ.setdefault(key, value)

from instagram import (
    InstagramClient,
    InstagramHTTPError,
    InstagramRateLimited,
    PublicPost,
)

# ============================================================================
# Test fixtures — realistic API v1 feed JSON
# ============================================================================

IMAGE_ITEM = {
    "code": "CxImg123AB",
    "caption": {"text": "A nice sunset 🌅"},
    "taken_at": 1700000000,
    "product_type": "feed",
    "media_type": 1,
    "image_versions2": {
        "candidates": [
            {"url": "https://scontent.cdninstagram.com/img_full.jpg", "width": 1080},
            {"url": "https://scontent.cdninstagram.com/img_small.jpg", "width": 320},
        ]
    },
}

REEL_ITEM = {
    "code": "CxReel456CD",
    "caption": {"text": "Check out this reel! 🎬"},
    "taken_at": 1700001000,
    "product_type": "clips",
    "media_type": 2,
    "video_versions": [
        {"url": "https://scontent.cdninstagram.com/reel_hd.mp4", "width": 1080},
        {"url": "https://scontent.cdninstagram.com/reel_sd.mp4", "width": 480},
    ],
    "image_versions2": {
        "candidates": [
            {"url": "https://scontent.cdninstagram.com/reel_thumb.jpg", "width": 1080},
        ]
    },
}

CAROUSEL_ITEM = {
    "code": "CxCarousel789",
    "caption": {"text": "A carousel post 📸"},
    "taken_at": 1700002000,
    "product_type": "feed",
    "media_type": 8,
    "carousel_media": [
        {
            "media_type": 1,
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.cdninstagram.com/car_1.jpg", "width": 1080},
                ]
            },
        },
        {
            "media_type": 2,
            "video_versions": [
                {"url": "https://scontent.cdninstagram.com/car_2.mp4", "width": 1080},
            ],
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.cdninstagram.com/car_2_thumb.jpg", "width": 1080},
                ]
            },
        },
        {
            "media_type": 1,
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.cdninstagram.com/car_3.jpg", "width": 1080},
                ]
            },
        },
    ],
    "image_versions2": {
        "candidates": [
            {"url": "https://scontent.cdninstagram.com/car_cover.jpg", "width": 1080},
        ]
    },
}

FEED_PAGE_1 = {
    "items": [CAROUSEL_ITEM, REEL_ITEM, IMAGE_ITEM],
    "more_available": True,
    "next_max_id": "page2cursor",
}

FEED_PAGE_2 = {
    "items": [
        {
            "code": "CxOlderPost01",
            "caption": {"text": "An older post"},
            "taken_at": 1699999000,
            "product_type": "feed",
            "media_type": 1,
            "image_versions2": {
                "candidates": [
                    {"url": "https://scontent.cdninstagram.com/old1.jpg", "width": 1080},
                ]
            },
        },
    ],
    "more_available": False,
}

PROFILE_HTML_WITH_ID = """
<html><head><script type="text/javascript">
window.__initialData = {"logging_page_id":"profilePage_1234567890",
"entry_data":{"ProfilePage":[{"graphql":{"user":{"id":"1234567890"}}}]}};
</script></head><body></body></html>
"""

PROFILE_HTML_WITHOUT_ID = """
<html><head></head><body><h1>Profile</h1></body></html>
"""

WEB_PROFILE_INFO_RESPONSE = {
    "data": {
        "user": {
            "id": "9876543210",
            "username": "testuser",
        }
    }
}


# ============================================================================
# Helper to build a minimal InstagramClient without real Instaloader
# ============================================================================

def _make_client(**kwargs):
    """Build an InstagramClient with Instaloader fully stubbed."""

    with patch("instagram.instaloader") as mock_il:
        # Stub the Instaloader constructor
        mock_loader = MagicMock()
        mock_loader.context._session = MagicMock(spec_set=["cookies", "proxies"])
        mock_loader.context._session.cookies = {}
        mock_loader.context._session.proxies = {}
        mock_loader.context.user_agent = ""
        mock_il.Instaloader.return_value = mock_loader
        mock_il.RateController = MagicMock

        client = InstagramClient(
            downloads_dir="temp_test_downloads",
            **kwargs,
        )
    return client


# ============================================================================
# Tests
# ============================================================================


class TestMediaItemToPost(unittest.TestCase):
    """Test _media_item_to_post static method."""

    def test_image_post(self):
        post = InstagramClient._media_item_to_post(IMAGE_ITEM)
        self.assertIsInstance(post, PublicPost)
        self.assertEqual(post.shortcode, "CxImg123AB")
        self.assertEqual(post.caption, "A nice sunset 🌅")
        self.assertIsNotNone(post.date_utc)
        self.assertEqual(post.date_utc, datetime.fromtimestamp(1700000000, tz=UTC))
        self.assertEqual(post.kind, "post")
        self.assertFalse(post.is_video)
        self.assertFalse(post.is_carousel)
        self.assertEqual(len(post.media_urls), 1)
        self.assertIn("img_full.jpg", post.media_urls[0])
        self.assertEqual(post.thumbnail_url, post.media_urls[0])

    def test_reel_video(self):
        post = InstagramClient._media_item_to_post(REEL_ITEM)
        self.assertIsInstance(post, PublicPost)
        self.assertEqual(post.shortcode, "CxReel456CD")
        self.assertEqual(post.kind, "reel")
        self.assertTrue(post.is_video)
        self.assertFalse(post.is_carousel)
        self.assertEqual(len(post.media_urls), 1)
        self.assertIn("reel_hd.mp4", post.media_urls[0])
        self.assertIsNotNone(post.thumbnail_url)
        self.assertIn("reel_thumb.jpg", post.thumbnail_url)

    def test_carousel(self):
        post = InstagramClient._media_item_to_post(CAROUSEL_ITEM)
        self.assertIsInstance(post, PublicPost)
        self.assertEqual(post.shortcode, "CxCarousel789")
        self.assertTrue(post.is_carousel)
        # Should have 3 media URLs (1 image + 1 video + 1 image from children)
        self.assertEqual(len(post.media_urls), 3)
        self.assertIn("car_1.jpg", post.media_urls[0])
        self.assertIn("car_2.mp4", post.media_urls[1])
        self.assertIn("car_3.jpg", post.media_urls[2])
        # Thumbnail comes from parent
        self.assertIn("car_cover.jpg", post.thumbnail_url)

    def test_missing_shortcode_returns_none(self):
        item = {**IMAGE_ITEM, "code": ""}
        self.assertIsNone(InstagramClient._media_item_to_post(item))

    def test_no_media_returns_none(self):
        item = {"code": "NoMedia123", "media_type": 1}
        self.assertIsNone(InstagramClient._media_item_to_post(item))


class TestExtractUserIdFromHtml(unittest.TestCase):
    """Test _extract_user_id_from_html static method."""

    def test_extracts_from_logging_page_id(self):
        user_id = InstagramClient._extract_user_id_from_html(PROFILE_HTML_WITH_ID)
        self.assertEqual(user_id, "1234567890")

    def test_returns_none_for_missing_id(self):
        user_id = InstagramClient._extract_user_id_from_html(PROFILE_HTML_WITHOUT_ID)
        self.assertIsNone(user_id)

    def test_profile_page_id_pattern(self):
        html = '<script>"profilePage_5551234567"</script>'
        self.assertEqual(InstagramClient._extract_user_id_from_html(html), "5551234567")

    def test_user_id_pattern(self):
        html = '<script>"user_id":"9988776655"</script>'
        self.assertEqual(InstagramClient._extract_user_id_from_html(html), "9988776655")

    def test_pk_pattern(self):
        html = '<script>"pk":"1122334455"</script>'
        self.assertEqual(InstagramClient._extract_user_id_from_html(html), "1122334455")

    def test_rejects_short_ids(self):
        html = '<script>"profilePage_123"</script>'
        self.assertIsNone(InstagramClient._extract_user_id_from_html(html))


class TestResolveUserId(unittest.TestCase):
    """Test _resolve_user_id with mocked HTTP."""

    def test_cached_value_returned(self):
        client = _make_client()
        client._user_id_cache["cached_user"] = "11111"
        self.assertEqual(client._resolve_user_id("cached_user"), "11111")

    def test_extracted_from_html(self):
        client = _make_client()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = PROFILE_HTML_WITH_ID

        with patch.object(client.session, "get", return_value=mock_resp):
            user_id = client._resolve_user_id("testuser")

        self.assertEqual(user_id, "1234567890")
        self.assertEqual(client._user_id_cache["testuser"], "1234567890")

    def test_falls_back_to_web_profile_info(self):
        client = _make_client()

        # First call: profile page returns no user ID
        html_resp = MagicMock()
        html_resp.status_code = 200
        html_resp.text = PROFILE_HTML_WITHOUT_ID

        # Second call: web_profile_info returns user data
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = WEB_PROFILE_INFO_RESPONSE

        api_session = MagicMock()
        api_session.get.return_value = api_resp

        with patch.object(client.session, "get", return_value=html_resp), \
             patch.object(client, "_get_api_session", return_value=api_session):
            user_id = client._resolve_user_id("testuser")

        self.assertEqual(user_id, "9876543210")

    def test_rate_limit_raises(self):
        client = _make_client()

        html_resp = MagicMock()
        html_resp.status_code = 200
        html_resp.text = PROFILE_HTML_WITHOUT_ID

        api_resp = MagicMock()
        api_resp.status_code = 429

        api_session = MagicMock()
        api_session.get.return_value = api_resp

        with patch.object(client.session, "get", return_value=html_resp), \
             patch.object(client, "_get_api_session", return_value=api_session):
            with self.assertRaises(InstagramRateLimited):
                client._resolve_user_id("testuser")


class TestFetchApiV1Feed(unittest.TestCase):
    """Test _fetch_api_v1_feed with mocked HTTP."""

    def test_single_page(self):
        client = _make_client()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {**FEED_PAGE_1, "more_available": False}

        api_session = MagicMock()
        api_session.get.return_value = resp

        with patch.object(client, "_get_api_session", return_value=api_session):
            posts = client._fetch_api_v1_feed("1234567890", limit=10)

        self.assertEqual(len(posts), 3)
        self.assertIsInstance(posts[0], PublicPost)
        self.assertEqual(posts[0].shortcode, "CxCarousel789")
        self.assertEqual(posts[1].shortcode, "CxReel456CD")
        self.assertEqual(posts[2].shortcode, "CxImg123AB")

    def test_pagination(self):
        client = _make_client()

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = FEED_PAGE_1

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = FEED_PAGE_2

        api_session = MagicMock()
        api_session.get.side_effect = [resp1, resp2]

        with patch.object(client, "_get_api_session", return_value=api_session):
            posts = client._fetch_api_v1_feed("1234567890", limit=10)

        self.assertEqual(len(posts), 4)
        self.assertEqual(posts[3].shortcode, "CxOlderPost01")
        # Verify two requests were made (page 1 + page 2)
        self.assertEqual(api_session.get.call_count, 2)

    def test_respects_limit(self):
        client = _make_client()

        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = FEED_PAGE_1

        api_session = MagicMock()
        api_session.get.return_value = resp

        with patch.object(client, "_get_api_session", return_value=api_session):
            posts = client._fetch_api_v1_feed("1234567890", limit=2)

        self.assertEqual(len(posts), 2)

    def test_rate_limit(self):
        client = _make_client()

        resp = MagicMock()
        resp.status_code = 429

        api_session = MagicMock()
        api_session.get.return_value = resp

        with patch.object(client, "_get_api_session", return_value=api_session):
            with self.assertRaises(InstagramRateLimited):
                client._fetch_api_v1_feed("1234567890", limit=5)


class TestLatestPosts(unittest.TestCase):
    """Test latest_posts() prefers API v1."""

    def test_returns_api_v1_posts(self):
        client = _make_client()

        with patch.object(client, "_resolve_user_id", return_value="1234567890"), \
             patch.object(client, "_fetch_api_v1_feed") as mock_feed:
            mock_feed.return_value = [
                PublicPost(shortcode="A1", media_urls=["http://x.jpg"]),
                PublicPost(shortcode="A2", media_urls=["http://y.jpg"]),
            ]
            posts = client.latest_posts("testuser", limit=5)

        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0].shortcode, "A1")
        mock_feed.assert_called_once_with("1234567890", 5)

    def test_falls_back_to_html_on_api_error(self):
        client = _make_client()

        with patch.object(
                client, "_resolve_user_id",
                side_effect=InstagramHTTPError("fail", transient=True),
             ), \
             patch.object(client, "fetch_public_posts") as mock_html:
            mock_html.return_value = [
                PublicPost(shortcode="H1", media_urls=["http://z.jpg"]),
            ]
            posts = client.latest_posts("testuser", limit=5)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].shortcode, "H1")

    def test_rate_limit_propagates(self):
        client = _make_client()

        with patch.object(
                client, "_resolve_user_id",
                side_effect=InstagramRateLimited("429"),
             ):
            with self.assertRaises(InstagramRateLimited):
                client.latest_posts("testuser", limit=5)


class TestIterPosts(unittest.TestCase):
    """Test iter_posts() yields newest -> oldest via API v1."""

    def test_yields_in_order(self):
        client = _make_client()

        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = FEED_PAGE_1

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = FEED_PAGE_2

        api_session = MagicMock()
        api_session.get.side_effect = [resp1, resp2]

        with patch.object(client, "_resolve_user_id", return_value="1234567890"), \
             patch.object(client, "_get_api_session", return_value=api_session):
            posts = list(client.iter_posts("testuser"))

        shortcodes = [p.shortcode for p in posts]
        self.assertEqual(
            shortcodes,
            ["CxCarousel789", "CxReel456CD", "CxImg123AB", "CxOlderPost01"],
        )
        # Verify newest first (taken_at descending in fixture data)
        for i in range(len(posts) - 1):
            if posts[i].date_utc and posts[i + 1].date_utc:
                self.assertGreaterEqual(posts[i].date_utc, posts[i + 1].date_utc)

    def test_deduplicates_shortcodes(self):
        client = _make_client()

        # Two pages returning the same shortcode
        page = {
            "items": [IMAGE_ITEM, IMAGE_ITEM],  # duplicate
            "more_available": False,
        }
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = page

        api_session = MagicMock()
        api_session.get.return_value = resp

        with patch.object(client, "_resolve_user_id", return_value="1234567890"), \
             patch.object(client, "_get_api_session", return_value=api_session):
            posts = list(client.iter_posts("testuser"))

        self.assertEqual(len(posts), 1)


class TestNoInstaLoaderSessionInjection(unittest.TestCase):
    """Verify Instaloader session is never replaced with curl_cffi."""

    def test_loader_session_is_standard_requests(self):
        """Instaloader must use a plain requests.Session, not curl_cffi."""

        import requests as std_requests

        client = _make_client()
        loader_session = client.loader.context._session

        # The mock sets up a MagicMock, but we verify the real Instaloader
        # session is never reassigned to self.session (curl_cffi).
        # self.session is an InstagramSession (potentially curl_cffi-based).
        self.assertIsNot(
            loader_session,
            client.session,
            "Instaloader session must NOT be the curl_cffi InstagramSession",
        )

    def test_api_session_is_standard_requests(self):
        """The API v1 session must be a plain requests.Session."""

        import requests as std_requests

        client = _make_client()
        api_session = client._get_api_session()

        self.assertIsInstance(api_session, std_requests.Session)


class TestGetApiSession(unittest.TestCase):
    """Test _get_api_session builds correctly."""

    def test_has_required_headers(self):
        client = _make_client()
        session = client._get_api_session()

        self.assertEqual(session.headers.get("X-IG-App-ID"), "936619743392459")
        self.assertEqual(session.headers.get("X-Requested-With"), "XMLHttpRequest")
        self.assertIn("User-Agent", session.headers)

    def test_caches_session(self):
        client = _make_client()
        s1 = client._get_api_session()
        s2 = client._get_api_session()
        self.assertIs(s1, s2)


if __name__ == "__main__":
    unittest.main()
