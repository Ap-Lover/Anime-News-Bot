from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_time_helpers():
    tree = ast.parse((ROOT / "bot.py").read_text(encoding="utf-8"))
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"parse_time_seconds", "format_duration"}
    ]
    namespace = {"re": re}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "bot.py", "exec"), namespace)
    return namespace["parse_time_seconds"], namespace["format_duration"]


class RepositoryTests(unittest.TestCase):
    def test_all_python_files_parse(self):
        for path in ROOT.rglob("*.py"):
            if any(part in {".venv", "__pycache__"} for part in path.parts):
                continue
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    def test_time_parser(self):
        parse_time_seconds, format_duration = load_time_helpers()
        self.assertEqual(parse_time_seconds("30"), 1800)
        self.assertEqual(parse_time_seconds("30m"), 1800)
        self.assertEqual(parse_time_seconds("1h"), 3600)
        self.assertEqual(parse_time_seconds("1800s"), 1800)
        self.assertEqual(format_duration(1800), "30m")
        with self.assertRaises(ValueError):
            parse_time_seconds("4m")
        with self.assertRaises(ValueError):
            parse_time_seconds("8d")

    def test_runtime_timer_is_wired(self):
        config = (ROOT / "config.py").read_text(encoding="utf-8")
        bot = (ROOT / "bot.py").read_text(encoding="utf-8")
        database = (ROOT / "database.py").read_text(encoding="utf-8")
        monitor = (ROOT / "monitor.py").read_text(encoding="utf-8")
        self.assertIn("POST_TIME_SECONDS", config)
        self.assertIn('Command("set_time")', bot)
        self.assertIn("set_monitor_interval", database)
        self.assertIn("get_monitor_interval", database)
        self.assertIn("monitor.wake()", bot)
        self.assertIn("self._wake", monitor)

    def test_github_safe_files_exist(self):
        for relative in [
            ".env.example",
            ".gitignore",
            ".github/workflows/ci.yml",
            "Dockerfile",
            "render.yaml",
            "README.md",
        ]:
            self.assertTrue((ROOT / relative).exists(), relative)
