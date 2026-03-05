#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate JSON Feed from enriched articles JSON")
    parser.add_argument("--input", required=True, type=Path, help="Input enriched JSON path")
    parser.add_argument("--output", required=True, type=Path, help="Output JSON Feed path")
    parser.add_argument("--title", default="Daily Tech Posts", help="Feed title")
    parser.add_argument("--home-page-url", default="", help="Feed home_page_url")
    parser.add_argument("--feed-url", default="", help="Feed self URL")
    return parser.parse_args()


def read_articles(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict) and isinstance(data.get("articles"), list):
        return [item for item in data["articles"] if isinstance(item, dict)]
    raise ValueError("Unsupported JSON shape. Expected list or {'articles': [...]} format.")


def article_id(item: dict[str, Any]) -> str:
    link = str(item.get("link") or "").strip()
    if link:
        return link
    payload = "|".join(
        (
            str(item.get("feed_url") or ""),
            str(item.get("title") or ""),
            str(item.get("published_at") or ""),
        )
    )
    return f"urn:sha256:{sha256(payload.encode('utf-8')).hexdigest()}"


def to_feed_item(item: dict[str, Any]) -> dict[str, Any]:
    title = str(item.get("title") or "(Untitled)")
    summary = str(item.get("summary") or "").strip()
    content = str(item.get("content") or "").strip()
    tags = item.get("tags")
    if not isinstance(tags, list):
        tags = []

    feed_item: dict[str, Any] = {
        "id": article_id(item),
        "title": title,
        "url": str(item.get("link") or ""),
        "date_published": str(item.get("published_at") or ""),
        "summary": summary,
        "content_text": summary or content,
        "tags": [str(tag) for tag in tags if str(tag).strip()],
    }
    feed_title = str(item.get("feed_title") or "").strip()
    if feed_title:
        feed_item["authors"] = [{"name": feed_title}]
    return feed_item


def build_json_feed(
    *,
    articles: list[dict[str, Any]],
    title: str,
    home_page_url: str,
    feed_url: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": title,
        "items": [to_feed_item(item) for item in articles],
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    if home_page_url:
        payload["home_page_url"] = home_page_url
    if feed_url:
        payload["feed_url"] = feed_url
    return payload


def main() -> int:
    args = parse_args()
    if not args.input.exists():
        raise SystemExit(f"Input file not found: {args.input}")

    articles = read_articles(args.input)
    articles.sort(key=lambda item: str(item.get("published_at") or ""), reverse=True)
    payload = build_json_feed(
        articles=articles,
        title=args.title,
        home_page_url=args.home_page_url,
        feed_url=args.feed_url,
    )

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote JSON Feed with {len(articles)} items to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
