#!/usr/bin/env python3
from __future__ import annotations

import argparse
import calendar
import json
import logging
import random
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

import feedparser
import requests

BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FEEDS_FILE = BASE_DIR / "feeds.txt"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
USER_AGENT = "daily-posts-rss-fetcher/1.0"


def load_feed_urls(feeds_file: Path) -> list[str]:
    if not feeds_file.exists():
        raise FileNotFoundError(
            f"feeds 文件不存在: {feeds_file}。请先从 feeds.txt.example 复制并填入 RSS URL。"
        )

    urls: list[str] = []
    for raw_line in feeds_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def parse_entry_datetime(entry: dict[str, Any]) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed_struct = entry.get(key)
        if parsed_struct:
            timestamp = calendar.timegm(parsed_struct)
            return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone()

    for key in ("published", "updated", "created"):
        raw_value = entry.get(key)
        if not raw_value:
            continue
        try:
            parsed_dt = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError):
            continue

        if parsed_dt.tzinfo is None:
            parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
        return parsed_dt.astimezone()

    return None


def extract_content(entry: dict[str, Any]) -> str:
    content_blocks = entry.get("content", [])
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if isinstance(block, dict):
                value = block.get("value")
                if value:
                    return str(value)

    for key in ("summary", "description"):
        value = entry.get(key)
        if value:
            return str(value)
    return ""


def configure_logging(log_file: Path | None) -> logging.Logger:
    logger = logging.getLogger("fetch_rss_today")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def _backoff_seconds(attempt: int, base_seconds: float) -> float:
    return base_seconds * (2 ** (attempt - 1)) + random.uniform(0.0, 0.2)


def fetch_and_parse_feed(
    *,
    url: str,
    timeout: int,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> feedparser.FeedParserDict:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
    }
    last_error: Exception | None = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, timeout=timeout, headers=headers)
            response.raise_for_status()
            parsed = feedparser.parse(response.content)
            if parsed.bozo and not parsed.entries:
                raise ValueError(f"feed 解析失败: {getattr(parsed, 'bozo_exception', 'unknown')}")
            return parsed
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            wait_seconds = _backoff_seconds(attempt, retry_base_seconds)
            logger.warning(
                "拉取失败，将重试 (%s/%s): %s (%s), %.2fs 后重试",
                attempt,
                max_retries,
                url,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)

    raise RuntimeError(f"拉取失败: {url} ({last_error})")


def collect_today_entries(
    *,
    feed_url: str,
    timeout: int,
    today: date,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], int, bool]:
    try:
        parsed = fetch_and_parse_feed(
            url=feed_url,
            timeout=timeout,
            max_retries=max_retries,
            retry_base_seconds=retry_base_seconds,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("拉取失败: %s (%s)", feed_url, exc)
        return [], 0, False

    feed_title = parsed.feed.get("title") or feed_url
    results: list[dict[str, Any]] = []
    parse_failures = 0

    for entry in parsed.entries:
        published_at = parse_entry_datetime(entry)
        if published_at is None:
            parse_failures += 1
            continue
        if published_at.date() != today:
            continue

        results.append(
            {
                "feed_title": str(feed_title),
                "feed_url": feed_url,
                "title": str(entry.get("title") or "(无标题)"),
                "link": str(entry.get("link") or ""),
                "published_at": published_at.isoformat(),
                "content": extract_content(entry),
            }
        )

    return results, parse_failures, True


def make_article_key(item: dict[str, Any]) -> str:
    link = str(item.get("link") or "").strip().lower()
    if link:
        return f"link:{link}"

    raw = "|".join(
        (
            str(item.get("feed_url") or ""),
            str(item.get("title") or ""),
            str(item.get("published_at") or ""),
        )
    )
    digest = sha256(raw.encode("utf-8")).hexdigest()
    return f"hash:{digest}"


def init_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS processed_articles (
            article_key TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL,
            title TEXT,
            link TEXT,
            feed_url TEXT,
            published_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_processed_articles_published_at
        ON processed_articles (published_at)
        """
    )
    conn.commit()
    return conn


def dedupe_with_db(
    *,
    items: list[dict[str, Any]],
    conn: sqlite3.Connection,
    now_iso: str,
) -> tuple[list[dict[str, Any]], int]:
    new_items: list[dict[str, Any]] = []
    duplicates = 0

    for item in items:
        article_key = make_article_key(item)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO processed_articles (
                article_key, first_seen_at, title, link, feed_url, published_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                article_key,
                now_iso,
                str(item.get("title") or ""),
                str(item.get("link") or ""),
                str(item.get("feed_url") or ""),
                str(item.get("published_at") or ""),
            ),
        )
        if cursor.rowcount == 1:
            new_items.append(item)
        else:
            duplicates += 1

    conn.commit()
    return new_items, duplicates


def save_results(items: list[dict[str, Any]], output_dir: Path, today: date, pretty: bool) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{today.isoformat()}.json"

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2 if pretty else None)
        f.write("\n")

    return output_path


def save_stats(stats_output: Path, payload: dict[str, Any]) -> None:
    stats_output.parent.mkdir(parents=True, exist_ok=True)
    stats_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="拉取 RSS 并筛选当天文章")
    parser.add_argument(
        "--feeds-file",
        type=Path,
        default=DEFAULT_FEEDS_FILE,
        help=f"RSS URL 清单文件，默认: {DEFAULT_FEEDS_FILE}",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"输出目录，默认: {DEFAULT_OUTPUT_DIR}",
    )
    parser.add_argument(
        "--date",
        dest="target_date",
        default=None,
        help="目标日期 (YYYY-MM-DD)，默认使用本地今天",
    )
    parser.add_argument("--timeout", type=int, default=20, help="请求超时秒数，默认 20")
    parser.add_argument("--max-retries", type=int, default=3, help="拉取重试次数，默认 3")
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=1.0,
        help="重试退避基准秒数，默认 1.0",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite 文件路径。设置后启用历史去重，避免重复处理",
    )
    parser.add_argument(
        "--stats-output",
        type=Path,
        default=None,
        help="可选：输出抓取统计 JSON 文件路径",
    )
    parser.add_argument("--log-file", type=Path, default=None, help="可选：日志文件路径")
    parser.add_argument("--pretty", action="store_true", help="JSON 使用缩进格式输出")
    return parser.parse_args()


def resolve_target_date(raw_target_date: str | None) -> date:
    if raw_target_date is None:
        return datetime.now().astimezone().date()
    return date.fromisoformat(raw_target_date)


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log_file)

    try:
        today = resolve_target_date(args.target_date)
    except ValueError:
        logger.error("无效日期格式: %s (需要 YYYY-MM-DD)", args.target_date)
        return 1

    try:
        feed_urls = load_feed_urls(args.feeds_file)
    except FileNotFoundError as exc:
        logger.error("%s", exc)
        return 1

    if not feed_urls:
        logger.warning("没有读取到 RSS URL: %s", args.feeds_file)

    all_items: list[dict[str, Any]] = []
    parse_failures = 0
    feed_failures = 0
    for url in feed_urls:
        items, failures, ok = collect_today_entries(
            feed_url=url,
            timeout=args.timeout,
            today=today,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            logger=logger,
        )
        if not ok:
            feed_failures += 1
        parse_failures += failures
        all_items.extend(items)

    # Guardrail: if every feed failed to fetch, stop early to avoid publishing an empty RSS
    # caused by transient DNS/network errors.
    if feed_urls and feed_failures == len(feed_urls):
        logger.error("所有 feed 拉取失败（%s/%s），终止本次流程以避免发布空 RSS。", feed_failures, len(feed_urls))
        return 2

    duplicates = 0
    if args.db_path is not None:
        try:
            conn = init_db(args.db_path)
            now_iso = datetime.now(timezone.utc).isoformat()
            all_items, duplicates = dedupe_with_db(items=all_items, conn=conn, now_iso=now_iso)
            conn.close()
            logger.info("SQLite 去重完成: duplicates=%s db=%s", duplicates, args.db_path)
        except sqlite3.Error as exc:
            logger.error("SQLite 去重失败: %s", exc)
            return 1

    all_items.sort(key=lambda item: item["published_at"], reverse=True)
    output_path = save_results(all_items, output_dir=args.output_dir, today=today, pretty=args.pretty)
    stats_output = args.stats_output or output_path.with_name(f"{today.isoformat()}.fetch.stats.json")
    stats_payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": today.isoformat(),
        "feeds_total": len(feed_urls),
        "feed_failures": feed_failures,
        "parse_failures": parse_failures,
        "duplicates_skipped": duplicates,
        "today_posts": len(all_items),
        "output": str(output_path),
        "db_path": str(args.db_path) if args.db_path else None,
    }
    save_stats(stats_output, stats_payload)
    logger.info(
        "完成: feeds=%s today_posts=%s parse_failures=%s duplicates=%s output=%s stats=%s",
        len(feed_urls),
        len(all_items),
        parse_failures,
        duplicates,
        output_path,
        stats_output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
