#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import format_datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE_DIR / "scripts"
PYTHON_BIN = sys.executable


class QuietRequestHandler(SimpleHTTPRequestHandler):
    def log_message(self, fmt: str, *args: object) -> None:
        _ = (fmt, args)


@dataclass
class LocalFeedServer:
    root: Path
    server: ThreadingHTTPServer
    thread: threading.Thread
    base_url: str

    @classmethod
    def start(cls, root: Path) -> "LocalFeedServer":
        handler = lambda *args, **kwargs: QuietRequestHandler(  # noqa: E731
            *args,
            directory=str(root),
            **kwargs,
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        host, port = server.server_address
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return cls(root=root, server=server, thread=thread, base_url=f"http://{host}:{port}")

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def write_sample_rss(path: Path, *, title: str, link: str, description: str, published_dt: datetime) -> None:
    published_rfc2822 = format_datetime(published_dt)
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Regression Feed</title>
    <link>http://localhost</link>
    <description>Regression test feed</description>
    <item>
      <title>{title}</title>
      <link>{link}</link>
      <pubDate>{published_rfc2822}</pubDate>
      <description>{description}</description>
    </item>
  </channel>
</rss>
"""
    path.write_text(rss, encoding="utf-8")


def write_source_html(path: Path, feed_href: str) -> None:
    html = f"""<!doctype html>
<html>
  <body>
    <a href="{feed_href}">Local Test Feed</a>
  </body>
</html>
"""
    path.write_text(html, encoding="utf-8")


def run_cmd(cmd: list[str], *, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd,
        cwd=BASE_DIR,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if result.returncode != 0:
        raise AssertionError(
            "Command failed:\n"
            f"cmd: {' '.join(cmd)}\n"
            f"exit: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def rss_item_count(path: Path) -> int:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    return len(root.findall("./channel/item"))


def read_opml_outline_attrs(path: Path) -> dict[str, str]:
    root = ET.fromstring(path.read_text(encoding="utf-8"))
    if root.tag != "opml":
        raise AssertionError("invalid OPML root tag")
    outline = root.find("./body/outline")
    if outline is None:
        raise AssertionError("missing OPML outline")
    return {k: v for k, v in outline.attrib.items()}


@contextmanager
def regression_workspace() -> Iterator[dict[str, Any]]:
    with tempfile.TemporaryDirectory(prefix="daily-posts-regression-") as tmp_dir:
        tmp = Path(tmp_dir)
        feed_root = tmp / "feed_server"
        feed_root.mkdir(parents=True, exist_ok=True)

        now_local = datetime.now().astimezone()
        today = now_local.date().isoformat()
        yesterday = (now_local.date() - timedelta(days=1)).isoformat()

        feed_file = feed_root / "today.xml"
        source_file = feed_root / "feeds.html"
        write_sample_rss(
            feed_file,
            title="Regression Test Post",
            link="https://example.com/regression-test-post",
            description="Regression test article body.",
            published_dt=now_local,
        )
        write_source_html(source_file, "/today.xml")

        server = LocalFeedServer.start(feed_root)
        try:
            yield {
                "tmp": tmp,
                "today": today,
                "yesterday": yesterday,
                "feed_url": f"{server.base_url}/today.xml",
                "source_url": f"{server.base_url}/feeds.html",
            }
        finally:
            server.stop()


class RegressionPipelineTests(unittest.TestCase):
    def test_run_daily_pipeline_non_empty(self) -> None:
        with regression_workspace() as ws:
            output_dir = ws["tmp"] / "output"
            log_dir = ws["tmp"] / "logs"
            data_dir = ws["tmp"] / "data"
            feeds_file = ws["tmp"] / "feeds.txt"
            feeds_file.write_text(f"{ws['feed_url']}\n", encoding="utf-8")

            run_cmd(
                [
                    PYTHON_BIN,
                    str(SCRIPTS_DIR / "run_daily_pipeline.py"),
                    "--feeds-file",
                    str(feeds_file),
                    "--output-dir",
                    str(output_dir),
                    "--log-dir",
                    str(log_dir),
                    "--db-path",
                    str(data_dir / "history.db"),
                    "--provider",
                    "none",
                    "--date",
                    ws["today"],
                    "--feed-title",
                    "Regression Daily Feed",
                ]
            )

            fetch_json = output_dir / f"{ws['today']}.json"
            feed_json = output_dir / f"{ws['today']}.feed.json"
            pipeline_stats = output_dir / f"{ws['today']}.pipeline.stats.json"

            self.assertTrue(fetch_json.exists(), "fetch output missing")
            self.assertTrue(feed_json.exists(), "feed output missing")
            self.assertTrue(pipeline_stats.exists(), "pipeline stats missing")

            fetch_items = read_json(fetch_json)
            feed_items = read_json(feed_json).get("items", [])

            self.assertEqual(len(fetch_items), 1, "expected one fetched article")
            self.assertEqual(len(feed_items), 1, "expected one feed item")

    def test_run_daily_pipeline_empty_for_previous_day(self) -> None:
        with regression_workspace() as ws:
            output_dir = ws["tmp"] / "output"
            log_dir = ws["tmp"] / "logs"
            data_dir = ws["tmp"] / "data"
            feeds_file = ws["tmp"] / "feeds.txt"
            feeds_file.write_text(f"{ws['feed_url']}\n", encoding="utf-8")

            run_cmd(
                [
                    PYTHON_BIN,
                    str(SCRIPTS_DIR / "run_daily_pipeline.py"),
                    "--feeds-file",
                    str(feeds_file),
                    "--output-dir",
                    str(output_dir),
                    "--log-dir",
                    str(log_dir),
                    "--db-path",
                    str(data_dir / "history.db"),
                    "--provider",
                    "none",
                    "--date",
                    ws["yesterday"],
                    "--feed-title",
                    "Regression Daily Feed",
                ]
            )

            fetch_json = output_dir / f"{ws['yesterday']}.json"
            feed_json = output_dir / f"{ws['yesterday']}.feed.json"
            ai_stats = output_dir / f"{ws['yesterday']}.ai.stats.json"

            self.assertTrue(fetch_json.exists(), "fetch output missing")
            self.assertTrue(feed_json.exists(), "feed output missing")
            self.assertTrue(ai_stats.exists(), "ai stats missing")

            fetch_items = read_json(fetch_json)
            feed_items = read_json(feed_json).get("items", [])
            ai_payload = read_json(ai_stats)

            self.assertEqual(len(fetch_items), 0, "expected zero fetched articles")
            self.assertEqual(len(feed_items), 0, "expected zero feed items")
            self.assertEqual(ai_payload.get("total_articles"), 0, "expected zero AI input articles")

    def test_run_full_pipeline_local_step1_to_step5(self) -> None:
        with regression_workspace() as ws:
            output_dir = ws["tmp"] / "output"
            published_dir = ws["tmp"] / "published"
            feeds_file = ws["tmp"] / "feeds.generated.txt"
            blog_json = ws["tmp"] / "blog_feeds.json"
            blog_csv = ws["tmp"] / "blog_feeds.csv"
            feed_output = output_dir / "daily_tech_feed.xml"
            opml_output = output_dir / "daily_tech_feed.opml"
            publish_path = published_dir / "daily_tech_feed.xml"
            opml_publish_path = published_dir / "daily_tech_feed.opml"

            env = os.environ.copy()
            env["OPENAI_API_KEY"] = ""

            run_cmd(
                [
                    PYTHON_BIN,
                    str(SCRIPTS_DIR / "run_full_pipeline.py"),
                    "--python-bin",
                    PYTHON_BIN,
                    "--source-url",
                    ws["source_url"],
                    "--step1-timeout",
                    "5",
                    "--fetch-timeout",
                    "5",
                    "--max-feeds",
                    "1",
                    "--skip-ai-if-no-key",
                    "--blog-json",
                    str(blog_json),
                    "--blog-csv",
                    str(blog_csv),
                    "--feeds-file",
                    str(feeds_file),
                    "--output-dir",
                    str(output_dir),
                    "--feed-output",
                    str(feed_output),
                    "--opml-output",
                    str(opml_output),
                    "--publish-path",
                    str(publish_path),
                    "--opml-publish-path",
                    str(opml_publish_path),
                    "--feed-link",
                    "https://example.com",
                    "--feed-self-link",
                    "https://example.com/daily_tech_feed.xml",
                    "--pretty",
                ],
                env=env,
            )

            raw_json = output_dir / f"{ws['today']}.json"
            enriched_json = output_dir / f"{ws['today']}.enriched.json"

            self.assertTrue(blog_json.exists(), "step1 JSON output missing")
            self.assertTrue(blog_csv.exists(), "step1 CSV output missing")
            self.assertTrue(feeds_file.exists(), "feeds file missing")
            self.assertTrue(raw_json.exists(), "step2 output missing")
            self.assertTrue(enriched_json.exists(), "step3 output missing")
            self.assertTrue(feed_output.exists(), "step4 RSS output missing")
            self.assertTrue(opml_output.exists(), "step4 OPML output missing")
            self.assertTrue(publish_path.exists(), "step5 publish copy missing")
            self.assertTrue(opml_publish_path.exists(), "step5 OPML publish copy missing")

            source_payload = read_json(blog_json)
            self.assertEqual(source_payload.get("count"), 1, "expected one feed source")
            self.assertEqual(source_payload.get("source"), ws["source_url"], "source URL mismatch")

            raw_items = read_json(raw_json)
            enriched_items = read_json(enriched_json)
            self.assertEqual(len(raw_items), 1, "expected one raw article")
            self.assertEqual(len(enriched_items), 1, "expected one enriched article")
            self.assertEqual(rss_item_count(feed_output), 1, "expected one RSS item")
            opml_outline = read_opml_outline_attrs(opml_output)
            self.assertEqual(opml_outline.get("type"), "rss", "OPML outline type mismatch")
            self.assertEqual(
                opml_outline.get("xmlUrl"),
                "https://example.com/daily_tech_feed.xml",
                "OPML xmlUrl mismatch",
            )
            self.assertEqual(feed_output.read_text(encoding="utf-8"), publish_path.read_text(encoding="utf-8"))
            self.assertEqual(opml_output.read_text(encoding="utf-8"), opml_publish_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
