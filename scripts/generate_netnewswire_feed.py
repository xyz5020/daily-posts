#!/usr/bin/env python3
"""Generate RSS feed (and optional OPML import file) from summarized article JSON."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from pathlib import Path
from typing import Any
import xml.etree.ElementTree as ET

LIST_CANDIDATE_KEYS = ("articles", "items", "entries", "results", "data", "posts")
TITLE_KEYS = ("title", "headline", "name")
URL_KEYS = ("url", "link", "source_url", "origin_url")
SUMMARY_KEYS = ("summary", "description", "abstract", "excerpt")
CONTENT_KEYS = ("content", "full_content", "body", "text")
DATE_KEYS = ("published", "published_at", "pub_date", "date", "created_at", "updated_at")
TAG_KEYS = ("tags", "tag_list", "labels", "keywords")


@dataclass
class Article:
    title: str
    url: str
    summary: str
    tags: list[str]
    content: str | None
    published_at: datetime


def _first_non_empty(record: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _extract_tags(record: dict[str, Any]) -> list[str]:
    for key in TAG_KEYS:
        raw = record.get(key)
        if raw is None:
            continue
        if isinstance(raw, str):
            return [part.strip() for part in raw.split(",") if part.strip()]
        if isinstance(raw, list):
            tags = []
            for item in raw:
                if isinstance(item, str) and item.strip():
                    tags.append(item.strip())
            return tags
    return []


def _parse_datetime(raw: Any) -> datetime:
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        candidate = raw.strip()
        if candidate:
            if candidate.endswith("Z"):
                candidate = candidate[:-1] + "+00:00"
            try:
                dt = datetime.fromisoformat(candidate)
                if dt.tzinfo is None:
                    return dt.replace(tzinfo=UTC)
                return dt.astimezone(UTC)
            except ValueError:
                pass
    return datetime.now(tz=UTC)


def _resolve_records(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]

    if isinstance(payload, dict):
        for key in LIST_CANDIDATE_KEYS:
            maybe = payload.get(key)
            if isinstance(maybe, list):
                return [x for x in maybe if isinstance(x, dict)]
        if any(k in payload for k in TITLE_KEYS + URL_KEYS):
            return [payload]
    return []


def _to_articles(records: list[dict[str, Any]]) -> list[Article]:
    articles: list[Article] = []
    seen: set[str] = set()

    for record in records:
        title = _first_non_empty(record, TITLE_KEYS)
        url = _first_non_empty(record, URL_KEYS)
        summary = _first_non_empty(record, SUMMARY_KEYS)
        content = _first_non_empty(record, CONTENT_KEYS) or None
        tags = _extract_tags(record)
        raw_date = None
        for key in DATE_KEYS:
            if key in record:
                raw_date = record[key]
                break
        published_at = _parse_datetime(raw_date)

        if not title or not url:
            continue

        dedupe_key = f"{title.lower()}::{url.lower()}"
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        articles.append(
            Article(
                title=title,
                url=url,
                summary=summary,
                tags=tags,
                content=content,
                published_at=published_at,
            )
        )

    articles.sort(key=lambda item: item.published_at, reverse=True)
    return articles


def _format_description(article: Article) -> str:
    lines = []
    if article.summary:
        lines.append(article.summary)
    if article.tags:
        lines.append(f"标签: {', '.join(article.tags)}")
    return "\n\n".join(lines) if lines else "暂无摘要"


def _build_guid(article: Article) -> str:
    normalized_tags = ",".join(sorted({tag.strip() for tag in article.tags if tag.strip()}))
    payload = "\n".join(
        (
            article.url.strip(),
            article.title.strip(),
            article.summary.strip(),
            (article.content or "").strip(),
            normalized_tags,
            article.published_at.isoformat(),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{article.url}#sha256:{digest}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Path to summarized article JSON")
    parser.add_argument(
        "--output",
        default="daily_tech_feed.xml",
        help="Path to generated RSS XML",
    )
    parser.add_argument(
        "--feed-title",
        default="Daily Tech Feed",
        help="RSS channel title",
    )
    parser.add_argument(
        "--feed-link",
        default="https://example.com",
        help="Website link for the channel",
    )
    parser.add_argument(
        "--feed-description",
        default="Daily summarized tech articles",
        help="RSS channel description",
    )
    parser.add_argument(
        "--feed-self-link",
        default="",
        help="Public URL of this RSS file (optional but recommended)",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=100,
        help="Maximum number of entries to include",
    )
    parser.add_argument(
        "--opml-output",
        default="",
        help="Optional path to generated OPML file for NetNewsWire import",
    )
    parser.add_argument(
        "--opml-title",
        default="Daily Tech Feed Subscriptions",
        help="OPML document title",
    )
    parser.add_argument(
        "--opml-feed-url",
        default="",
        help="Feed URL used in OPML outline xmlUrl (default: --feed-self-link or local RSS file URI)",
    )
    parser.add_argument(
        "--include-full-content",
        action="store_true",
        help="Include article full content in RSS <content:encoded>. Default is summary-only.",
    )
    return parser.parse_args()


def _write_opml(
    *,
    output_path: Path,
    opml_title: str,
    feed_title: str,
    feed_url: str,
    html_url: str,
) -> None:
    opml = ET.Element("opml", attrib={"version": "2.0"})
    head = ET.SubElement(opml, "head")
    ET.SubElement(head, "title").text = opml_title
    ET.SubElement(head, "dateCreated").text = format_datetime(datetime.now(tz=UTC))

    body = ET.SubElement(opml, "body")
    outline_attrs = {
        "text": feed_title,
        "title": feed_title,
        "type": "rss",
        "xmlUrl": feed_url,
    }
    if html_url:
        outline_attrs["htmlUrl"] = html_url
    ET.SubElement(body, "outline", attrib=outline_attrs)

    tree = ET.ElementTree(opml)
    if hasattr(ET, "indent"):
        ET.indent(tree, space="  ")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)


def main() -> int:
    try:
        from feedgen.feed import FeedGenerator
    except ModuleNotFoundError:
        print(
            "[ERROR] Missing dependency: feedgen. Install dependencies first: "
            "pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 1

    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    opml_output_path = Path(args.opml_output) if args.opml_output else None

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    records = _resolve_records(payload)
    articles = _to_articles(records)
    if args.max_items > 0:
        articles = articles[: args.max_items]

    fg = FeedGenerator()
    fg.title(args.feed_title)
    fg.description(args.feed_description)
    fg.link(href=args.feed_link, rel="alternate")
    fg.language("zh-CN")

    if args.feed_self_link:
        fg.link(href=args.feed_self_link, rel="self")

    now = datetime.now(tz=UTC)
    fg.pubDate(format_datetime(now))
    fg.lastBuildDate(format_datetime(now))

    for article in articles:
        entry = fg.add_entry()
        entry.title(f"{article.title} 摘要")
        entry.link(href=article.url)
        entry.guid(_build_guid(article), permalink=False)
        entry.pubDate(format_datetime(article.published_at))

        description = _format_description(article)
        entry.description(description)

        if args.include_full_content and article.content:
            entry.content(article.content, type="html")

        for tag in article.tags:
            entry.category(term=tag)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fg.rss_file(str(output_path), pretty=True)

    print(f"[OK] Generated {len(articles)} entries to {output_path}")

    if opml_output_path is not None:
        opml_feed_url = args.opml_feed_url.strip()
        if not opml_feed_url:
            if args.feed_self_link:
                opml_feed_url = args.feed_self_link.strip()
            else:
                opml_feed_url = output_path.resolve().as_uri()
        _write_opml(
            output_path=opml_output_path,
            opml_title=args.opml_title,
            feed_title=args.feed_title,
            feed_url=opml_feed_url,
            html_url=args.feed_link.strip(),
        )
        print(f"[OK] Generated OPML import file to {opml_output_path} (xmlUrl={opml_feed_url})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
