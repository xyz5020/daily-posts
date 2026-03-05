#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = BASE_DIR / "scripts"


def write_sample_rss(path: Path) -> None:
    now_rfc2822 = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S %z")
    rss = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Smoke Test Feed</title>
    <link>http://localhost</link>
    <description>Daily posts smoke test</description>
    <item>
      <title>Test Post 1</title>
      <link>https://example.com/test-post-1</link>
      <pubDate>{now_rfc2822}</pubDate>
      <description>Hello step5 smoke test content.</description>
    </item>
  </channel>
</rss>
"""
    path.write_text(rss, encoding="utf-8")


def write_stub_modules(path: Path, rss_xml: str) -> None:
    requests_stub = f"""class RequestException(Exception):
    pass

class _Response:
    def __init__(self, content: bytes):
        self.content = content

    def raise_for_status(self):
        return None

def get(url, timeout=20, headers=None):
    _ = (url, timeout, headers)
    return _Response(content={rss_xml!r}.encode("utf-8"))
"""
    feedparser_stub = """import xml.etree.ElementTree as ET

class Parsed:
    def __init__(self, feed, entries, bozo=False):
        self.feed = feed
        self.entries = entries
        self.bozo = bozo

def parse(content):
    root = ET.fromstring(content)
    channel = root.find("channel")
    feed_title = ""
    entries = []
    if channel is not None:
        feed_title = channel.findtext("title", default="")
        for item in channel.findall("item"):
            entries.append(
                {
                    "title": item.findtext("title", default=""),
                    "link": item.findtext("link", default=""),
                    "published": item.findtext("pubDate", default=""),
                    "description": item.findtext("description", default=""),
                }
            )
    return Parsed(feed={"title": feed_title}, entries=entries, bozo=False)
"""
    (path / "requests.py").write_text(requests_stub, encoding="utf-8")
    (path / "feedparser.py").write_text(feedparser_stub, encoding="utf-8")


def run_cmd(cmd: list[str], env: dict[str, str] | None = None) -> None:
    print("$", " ".join(cmd))
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(cmd, capture_output=True, text=True, check=False, env=merged_env)
    if result.stdout.strip():
        print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"Command failed ({result.returncode}): {' '.join(cmd)}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="daily-posts-step5-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        data_dir = tmp_path / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        rss_path = tmp_path / "sample.xml"
        write_sample_rss(rss_path)
        rss_xml = rss_path.read_text(encoding="utf-8")
        stubs_dir = tmp_path / "stubs"
        stubs_dir.mkdir(parents=True, exist_ok=True)
        write_stub_modules(stubs_dir, rss_xml)

        feed_url = "https://example.local/smoke-rss.xml"
        feeds_file = tmp_path / "feeds.txt"
        feeds_file.write_text(feed_url + "\n", encoding="utf-8")
        target_date = datetime.now().astimezone().date().isoformat()
        extra_env = {"PYTHONPATH": str(stubs_dir)}

        run_cmd(
            [
                sys.executable,
                str(SCRIPTS_DIR / "fetch_rss_today.py"),
                "--feeds-file",
                str(feeds_file),
                "--output-dir",
                str(output_dir),
                "--date",
                target_date,
                "--db-path",
                str(data_dir / "history.db"),
                "--stats-output",
                str(output_dir / f"{target_date}.fetch.stats.json"),
                "--log-file",
                str(logs_dir / "fetch.log"),
                "--pretty",
            ],
            env=extra_env,
        )

        fetch_output = output_dir / f"{target_date}.json"
        enriched_output = output_dir / f"{target_date}.enriched.json"
        feed_output = output_dir / f"{target_date}.feed.json"

        run_cmd(
            [
                sys.executable,
                str(SCRIPTS_DIR / "step3_ai_extract.py"),
                "--input",
                str(fetch_output),
                "--output",
                str(enriched_output),
                "--provider",
                "none",
                "--stats-output",
                str(output_dir / f"{target_date}.ai.stats.json"),
                "--log-file",
                str(logs_dir / "ai.log"),
            ]
        )

        run_cmd(
            [
                sys.executable,
                str(SCRIPTS_DIR / "generate_json_feed.py"),
                "--input",
                str(enriched_output),
                "--output",
                str(feed_output),
                "--title",
                "Smoke Test Feed Output",
            ]
        )

        fetch_items = json.loads(fetch_output.read_text(encoding="utf-8"))
        enriched_items = json.loads(enriched_output.read_text(encoding="utf-8"))
        generated_feed = json.loads(feed_output.read_text(encoding="utf-8"))

        assert isinstance(fetch_items, list) and len(fetch_items) == 1
        assert isinstance(enriched_items, list) and len(enriched_items) == 1
        assert isinstance(generated_feed, dict) and len(generated_feed.get("items", [])) == 1
        print("Smoke test passed: fetch -> AI extract -> feed generation")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
