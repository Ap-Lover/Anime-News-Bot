from __future__ import annotations

import unittest

try:
    from uploader import sanitize_caption
except ModuleNotFoundError as exc:
    sanitize_caption = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


@unittest.skipUnless(
    sanitize_caption is not None,
    f"optional runtime dependencies are unavailable: {_IMPORT_ERROR}",
)
class CaptionSanitizerTests(unittest.TestCase):
    def test_removes_mentions_and_urls(self):
        text = "Anime update @sastaotaku https://instagram.com/x example.com t.me/channel #anime"
        cleaned = sanitize_caption(text)
        self.assertNotIn("@sastaotaku", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("example.com", cleaned)
        self.assertNotIn("t.me/channel", cleaned)
        self.assertIn("#anime", cleaned)

    def test_normalizes_blank_lines(self):
        self.assertEqual(sanitize_caption("Hello\n\n  world  "), "Hello\nworld")
