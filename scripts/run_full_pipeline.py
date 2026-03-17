#!/usr/bin/env python3
"""Run steps 1 to 5 in sequence and produce publishable RSS/OPML artifacts."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Sequence
from xml.etree import ElementTree as ET


BASE_DIR = Path(__file__).resolve().parents[1]

STEP1_SCRIPT = BASE_DIR / "scripts" / "fetch_dailybit_feeds.py"
STEP2_SCRIPT = BASE_DIR / "scripts" / "fetch_rss_today.py"
STEP3_SCRIPT = BASE_DIR / "scripts" / "step3_ai_extract.py"
STEP4_SCRIPT = BASE_DIR / "scripts" / "generate_netnewswire_feed.py"

DEFAULT_SOURCE_URL = "https://www.dailybit.cc/feeds"
DEFAULT_BLOG_JSON = BASE_DIR / "data" / "blog_feeds.json"
DEFAULT_BLOG_CSV = BASE_DIR / "data" / "blog_feeds.csv"
DEFAULT_FEEDS_FILE = BASE_DIR / "feeds.txt"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_FEED_OUTPUT = DEFAULT_OUTPUT_DIR / "daily_tech_feed.xml"
DEFAULT_OPML_OUTPUT = DEFAULT_OUTPUT_DIR / "daily_tech_feed.opml"
DEFAULT_PUBLISH_PATH = BASE_DIR / "daily_tech_feed.xml"
DEFAULT_OPML_PUBLISH_PATH = BASE_DIR / "daily_tech_feed.opml"

LIST_CANDIDATE_KEYS = ("articles", "items", "entries", "results", "data", "posts")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按步骤1~5顺序执行：抓源 -> 拉取当天 -> AI摘要 -> 生成RSS/OPML -> 发布复制。"
    )
    parser.add_argument("--python-bin", default=sys.executable, help="Python binary used for sub-steps")
    parser.add_argument("--allow-fallback", action="store_true", help="Step 1 source unavailable 时使用内置回退")
    parser.add_argument("--source-url", default=DEFAULT_SOURCE_URL, help="Step 1 source page URL")
    parser.add_argument("--step1-timeout", type=int, default=20, help="Step 1 request timeout in seconds")
    parser.add_argument("--blog-json", type=Path, default=DEFAULT_BLOG_JSON, help="Step 1 JSON output path")
    parser.add_argument("--blog-csv", type=Path, default=DEFAULT_BLOG_CSV, help="Step 1 CSV output path")
    parser.add_argument("--feeds-file", type=Path, default=DEFAULT_FEEDS_FILE, help="Step 2 feeds.txt path")
    parser.add_argument(
        "--feeds-opml",
        type=Path,
        default=None,
        help="Use OPML xmlUrl subscriptions as feed source and skip Step 1 web fetch",
    )
    parser.add_argument("--max-feeds", type=int, default=0, help="Limit feed count written into feeds.txt (0 = all)")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Step 2/3/4 output directory")
    parser.add_argument("--fetch-timeout", type=int, default=20, help="Step 2 request timeout in seconds")
    parser.add_argument("--pretty", action="store_true", help="Step 2 JSON pretty output")
    parser.add_argument(
        "--enriched-output",
        type=Path,
        default=None,
        help="Step 3 output JSON path (default: output/YYYY-MM-DD.enriched.json)",
    )
    parser.add_argument("--model", default="gpt-4.1-mini", help="Step 3 OpenAI model name")
    parser.add_argument("--max-input-chars", type=int, default=12000, help="Step 3 max input characters per article")
    parser.add_argument("--sleep-seconds", type=float, default=0.3, help="Step 3 sleep between API calls")
    parser.add_argument(
        "--skip-ai-if-no-key",
        action="store_true",
        help="If OPENAI_API_KEY is missing, skip Step 3 and continue",
    )
    parser.add_argument("--feed-output", type=Path, default=DEFAULT_FEED_OUTPUT, help="Step 4 RSS XML output path")
    parser.add_argument("--feed-title", default="Daily Tech 摘要", help="Step 4 feed title")
    parser.add_argument("--feed-link", default="https://example.com", help="Step 4 feed site link")
    parser.add_argument("--feed-description", default="每日技术文章摘要", help="Step 4 feed description")
    parser.add_argument("--feed-self-link", default="", help="Step 4 feed self link")
    parser.add_argument("--max-items", type=int, default=100, help="Step 4 max RSS entries")
    parser.add_argument("--opml-output", type=Path, default=DEFAULT_OPML_OUTPUT, help="Step 4 OPML output path")
    parser.add_argument("--opml-title", default="Daily Tech Feed Subscriptions", help="Step 4 OPML title")
    parser.add_argument("--opml-feed-url", default="", help="Step 4 OPML xmlUrl override (default uses feed-self-link)")
    parser.add_argument("--skip-opml", action="store_true", help="Skip Step 4 OPML generation")
    parser.add_argument("--skip-publish-copy", action="store_true", help="Skip Step 5 copy to publish path")
    parser.add_argument("--publish-path", type=Path, default=DEFAULT_PUBLISH_PATH, help="Step 5 publish file path")
    parser.add_argument(
        "--opml-publish-path",
        type=Path,
        default=DEFAULT_OPML_PUBLISH_PATH,
        help="Step 5 OPML publish file path",
    )
    return parser.parse_args()


def run_step(step_name: str, cmd: Sequence[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    print(f"[{step_name}] {' '.join(cmd)}", flush=True)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, check=False)

    if result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr, flush=True)

    if result.returncode != 0:
        raise RuntimeError(f"{step_name} failed with exit code {result.returncode}")
    return result


def load_feed_urls(blog_json: Path, max_feeds: int) -> list[str]:
    payload = json.loads(blog_json.read_text(encoding="utf-8"))
    feeds = payload.get("feeds") if isinstance(payload, dict) else None
    if not isinstance(feeds, list):
        raise RuntimeError(f"Invalid feed JSON format: {blog_json}")

    urls: list[str] = []
    seen: set[str] = set()
    for item in feeds:
        if not isinstance(item, dict):
            continue
        rss_url = str(item.get("rss_url") or "").strip()
        if not rss_url:
            continue
        key = rss_url.lower()
        if key in seen:
            continue
        seen.add(key)
        urls.append(rss_url)
        if max_feeds > 0 and len(urls) >= max_feeds:
            break

    if not urls:
        raise RuntimeError(f"No feed URLs found in {blog_json}")
    return urls


def write_feeds_txt(urls: list[str], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(urls) + "\n", encoding="utf-8")


def load_feed_rows_from_opml(opml_path: Path, max_feeds: int) -> list[dict[str, str]]:
    if not opml_path.exists():
        raise FileNotFoundError(f"OPML file not found: {opml_path}")

    try:
        root = ET.parse(opml_path).getroot()
    except ET.ParseError as exc:
        raise RuntimeError(f"Invalid OPML format: {opml_path} ({exc})") from exc

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for outline in root.findall(".//outline"):
        rss_url = str(
            outline.attrib.get("xmlUrl")
            or outline.attrib.get("xmlurl")
            or outline.attrib.get("url")
            or ""
        ).strip()
        if not rss_url:
            continue

        key = rss_url.lower()
        if key in seen:
            continue
        seen.add(key)

        name = str(outline.attrib.get("title") or outline.attrib.get("text") or "").strip() or rss_url
        rows.append({"name": name, "rss_url": rss_url})
        if max_feeds > 0 and len(rows) >= max_feeds:
            break

    if not rows:
        raise RuntimeError(f"No feed xmlUrl entries found in OPML: {opml_path}")
    return rows


def write_feed_catalog(blog_json: Path, blog_csv: Path, rows: list[dict[str, str]], source_tag: str) -> None:
    blog_json.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source_tag,
        "count": len(rows),
        "feeds": rows,
    }
    blog_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    blog_csv.parent.mkdir(parents=True, exist_ok=True)
    with blog_csv.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "rss_url"])
        for row in rows:
            writer.writerow([row["name"], row["rss_url"]])


def detect_step2_output(result: subprocess.CompletedProcess[str], fallback: Path) -> Path:
    combined = f"{result.stdout}\n{result.stderr}"
    match = re.search(r"output=([^\s]+\.json)", combined)
    if match:
        candidate = Path(match.group(1)).expanduser()
        if not candidate.is_absolute():
            return (Path.cwd() / candidate).resolve()
        return candidate
    return fallback


def count_records(json_path: Path) -> int:
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        return len([x for x in payload if isinstance(x, dict)])
    if isinstance(payload, dict):
        for key in LIST_CANDIDATE_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return len([x for x in value if isinstance(x, dict)])
        return 1
    return 0


def check_required_dependencies(python_bin: str) -> None:
    check_cmd = [
        python_bin,
        "-c",
        "import feedparser, requests, feedgen",
    ]
    result = subprocess.run(check_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        hint = "Please install dependencies first: pip install -r requirements.txt"
        if stderr:
            raise RuntimeError(f"Missing dependencies. {hint}. Details: {stderr}")
        raise RuntimeError(f"Missing dependencies. {hint}.")


def main() -> int:
    args = parse_args()

    blog_json = args.blog_json
    blog_csv = args.blog_csv
    feeds_file = args.feeds_file
    output_dir = args.output_dir
    feed_output = args.feed_output
    opml_output = args.opml_output
    publish_path = args.publish_path
    opml_publish_path = args.opml_publish_path

    try:
        check_required_dependencies(args.python_bin)

        if args.feeds_opml is not None:
            opml_path = args.feeds_opml.expanduser()
            if not opml_path.is_absolute():
                opml_path = (Path.cwd() / opml_path).resolve()
            rows = load_feed_rows_from_opml(opml_path, max_feeds=args.max_feeds)
            write_feed_catalog(blog_json, blog_csv, rows, source_tag=f"opml:{opml_path}")
            urls = [row["rss_url"] for row in rows]
            write_feeds_txt(urls, feeds_file)
            print(f"[step1] loaded {len(rows)} feeds from OPML: {opml_path}")
            print(f"[step2] wrote {len(urls)} urls to {feeds_file}")
        else:
            step1_cmd = [
                args.python_bin,
                str(STEP1_SCRIPT),
                "--url",
                args.source_url,
                "--json-out",
                str(blog_json),
                "--csv-out",
                str(blog_csv),
                "--timeout",
                str(args.step1_timeout),
            ]
            if args.allow_fallback:
                step1_cmd.append("--allow-fallback")
            run_step("step1", step1_cmd)

            urls = load_feed_urls(blog_json, max_feeds=args.max_feeds)
            write_feeds_txt(urls, feeds_file)
            print(f"[step2] wrote {len(urls)} urls to {feeds_file}")

        step2_cmd = [
            args.python_bin,
            str(STEP2_SCRIPT),
            "--feeds-file",
            str(feeds_file),
            "--output-dir",
            str(output_dir),
            "--timeout",
            str(args.fetch_timeout),
        ]
        if args.pretty:
            step2_cmd.append("--pretty")
        step2_result = run_step("step2", step2_cmd)

        today_filename = f"{datetime.now().astimezone().date().isoformat()}.json"
        raw_articles_path = detect_step2_output(step2_result, fallback=output_dir / today_filename)
        if not raw_articles_path.exists():
            raise RuntimeError(f"Step 2 output file not found: {raw_articles_path}")
        print(f"[step2] raw article file: {raw_articles_path}")

        enriched_output = args.enriched_output
        if enriched_output is None:
            enriched_output = output_dir / f"{raw_articles_path.stem}.enriched.json"

        record_count = count_records(raw_articles_path)
        if record_count == 0:
            enriched_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_articles_path, enriched_output)
            print(f"[step3] no articles today, copied {raw_articles_path} -> {enriched_output}")
        elif not os.getenv("OPENAI_API_KEY") and args.skip_ai_if_no_key:
            enriched_output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(raw_articles_path, enriched_output)
            print("[step3] OPENAI_API_KEY missing, skipped AI extraction by --skip-ai-if-no-key")
        else:
            step3_cmd = [
                args.python_bin,
                str(STEP3_SCRIPT),
                "--input",
                str(raw_articles_path),
                "--output",
                str(enriched_output),
                "--model",
                args.model,
                "--max-input-chars",
                str(args.max_input_chars),
                "--sleep-seconds",
                str(args.sleep_seconds),
            ]
            run_step("step3", step3_cmd)

        step4_cmd = [
            args.python_bin,
            str(STEP4_SCRIPT),
            "--input",
            str(enriched_output),
            "--output",
            str(feed_output),
            "--feed-title",
            args.feed_title,
            "--feed-link",
            args.feed_link,
            "--feed-description",
            args.feed_description,
            "--max-items",
            str(args.max_items),
        ]
        if args.feed_self_link:
            step4_cmd.extend(["--feed-self-link", args.feed_self_link])
        if not args.skip_opml:
            step4_cmd.extend(["--opml-output", str(opml_output)])
            step4_cmd.extend(["--opml-title", args.opml_title])
            if args.opml_feed_url:
                step4_cmd.extend(["--opml-feed-url", args.opml_feed_url])
        run_step("step4", step4_cmd)

        if args.skip_publish_copy:
            print("[step5] skipped publish copy by --skip-publish-copy")
        else:
            publish_path.parent.mkdir(parents=True, exist_ok=True)
            if feed_output.resolve() == publish_path.resolve():
                print(f"[step5] publish path equals feed output, skip copy: {publish_path}")
            else:
                shutil.copyfile(feed_output, publish_path)
                print(f"[step5] copied {feed_output} -> {publish_path}")

            if not args.skip_opml:
                opml_publish_path.parent.mkdir(parents=True, exist_ok=True)
                if opml_output.resolve() == opml_publish_path.resolve():
                    print(f"[step5] OPML publish path equals OPML output, skip copy: {opml_publish_path}")
                else:
                    shutil.copyfile(opml_output, opml_publish_path)
                    print(f"[step5] copied {opml_output} -> {opml_publish_path}")

        print("[done] steps 1-5 finished successfully")
        return 0
    except RuntimeError as err:
        print(f"[ERROR] {err}", file=sys.stderr)
        return 1
    except FileNotFoundError as err:
        print(f"[ERROR] File not found: {err}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as err:
        print(f"[ERROR] JSON parse failed: {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
