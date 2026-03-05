#!/usr/bin/env python3
"""Fetch and normalize tech blog RSS feeds into JSON/CSV files.

Default source page:
https://www.dailybit.cc/feeds
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


DEFAULT_SOURCE_URL = "https://www.dailybit.cc/feeds"

# Fallback list keeps step 1 usable when network/source is temporarily unavailable.
FALLBACK_FEEDS = [
    {"name": "Hacker News", "rss_url": "https://hnrss.org/frontpage"},
    {"name": "Dev.to", "rss_url": "https://dev.to/feed"},
    {"name": "GitHub Blog", "rss_url": "https://github.blog/feed/"},
    {"name": "Smashing Magazine", "rss_url": "https://www.smashingmagazine.com/feed/"},
    {"name": "CSS-Tricks", "rss_url": "https://css-tricks.com/feed/"},
    {"name": "InfoQ", "rss_url": "https://www.infoq.com/feed/"},
    {"name": "Martin Fowler", "rss_url": "https://martinfowler.com/feed.atom"},
    {"name": "The Pragmatic Engineer", "rss_url": "https://newsletter.pragmaticengineer.com/feed"},
]


def looks_like_feed_url(url: str) -> bool:
    lowered = url.lower()
    return any(
        token in lowered
        for token in (
            "/feed",
            "rss",
            "atom",
            ".xml",
            "hnrss.org",
        )
    )


def normalize_name(raw_name: str, rss_url: str) -> str:
    raw_name = re.sub(r"\s+", " ", raw_name).strip()
    if raw_name:
        return raw_name
    parsed = urlparse(rss_url)
    return parsed.netloc or rss_url


@dataclass
class FeedItem:
    name: str
    rss_url: str


class FeedLinkParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._in_a = False
        self._current_href: str | None = None
        self._text_buffer: list[str] = []
        self.items: list[FeedItem] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = None
        for key, value in attrs:
            if key.lower() == "href":
                href = value
                break
        if not href:
            return
        self._in_a = True
        self._current_href = urljoin(self.base_url, href.strip())
        self._text_buffer = []

    def handle_data(self, data: str) -> None:
        if self._in_a:
            self._text_buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self._in_a or not self._current_href:
            return
        feed_url = self._current_href
        text = "".join(self._text_buffer)
        if looks_like_feed_url(feed_url):
            self.items.append(FeedItem(name=normalize_name(text, feed_url), rss_url=feed_url))
        self._in_a = False
        self._current_href = None
        self._text_buffer = []


def fetch_html(url: str, timeout: int) -> str:
    req = Request(url=url, headers={"User-Agent": "daily-posts-feed-fetcher/1.0"})
    with urlopen(req, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def dedupe(items: Iterable[FeedItem]) -> list[FeedItem]:
    seen: set[str] = set()
    deduped: list[FeedItem] = []
    for item in items:
        key = item.rss_url.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def parse_feeds_from_html(html: str, base_url: str) -> list[FeedItem]:
    parser = FeedLinkParser(base_url=base_url)
    parser.feed(html)
    return dedupe(parser.items)


def write_json(path: Path, rows: list[FeedItem], source_url: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source": source_url,
        "count": len(rows),
        "feeds": [{"name": row.name, "rss_url": row.rss_url} for row in rows],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, rows: list[FeedItem]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["name", "rss_url"])
        for row in rows:
            writer.writerow([row.name, row.rss_url])


def as_feed_items(entries: list[dict[str, str]]) -> list[FeedItem]:
    rows: list[FeedItem] = []
    for entry in entries:
        name = entry.get("name", "").strip()
        rss_url = entry.get("rss_url", "").strip()
        if not name or not rss_url:
            continue
        rows.append(FeedItem(name=name, rss_url=rss_url))
    return rows


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_SOURCE_URL, help="Source page containing feed links")
    parser.add_argument(
        "--json-out",
        default="data/blog_feeds.json",
        help="Output path for JSON list",
    )
    parser.add_argument(
        "--csv-out",
        default="data/blog_feeds.csv",
        help="Output path for CSV list",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=20,
        help="HTTP request timeout in seconds",
    )
    parser.add_argument(
        "--allow-fallback",
        action="store_true",
        help="Use built-in official feeds when source page is unreachable or empty",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_url = args.url
    rows: list[FeedItem]

    try:
        html = fetch_html(source_url, timeout=args.timeout)
        rows = parse_feeds_from_html(html, base_url=source_url)
        if not rows:
            raise ValueError("No feed links found on source page")
        source_tag = source_url
    except (URLError, TimeoutError, ValueError) as exc:
        if not args.allow_fallback:
            print(f"[ERROR] Failed to fetch/parse feeds from {source_url}: {exc}", file=sys.stderr)
            print("Hint: rerun with --allow-fallback to generate a usable starter list.", file=sys.stderr)
            return 1
        rows = as_feed_items(FALLBACK_FEEDS)
        source_tag = f"fallback:{source_url}"
        print(f"[WARN] Using fallback feeds because source is unavailable: {exc}", file=sys.stderr)

    rows = dedupe(rows)
    write_json(Path(args.json_out), rows, source_tag)
    write_csv(Path(args.csv_out), rows)
    print(f"[OK] Wrote {len(rows)} feeds to {args.json_out} and {args.csv_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

