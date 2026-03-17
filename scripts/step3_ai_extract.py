#!/usr/bin/env python3
"""Step 3: generate summary and tags for each article with AI."""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OPENAI_CHAT_COMPLETIONS_URL = "https://api.openai.com/v1/chat/completions"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_PROVIDER = "openai"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
BASE_SYSTEM_PROMPT = (
    "You generate high-quality article metadata for indexing. "
    "Always return valid JSON object."
)
PRICING_PER_1M_TOKENS = {
    "gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
}
TAG_SPLIT_RE = re.compile(r"[\n,;|]+")
JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9#+\-.]{2,24}")
STOPWORDS = {
    "the",
    "and",
    "for",
    "that",
    "with",
    "this",
    "from",
    "your",
    "have",
    "will",
    "are",
    "was",
    "were",
    "into",
    "about",
    "https",
    "http",
    "www",
    "com",
    "article",
    "their",
    "they",
    "them",
    "than",
    "over",
    "under",
    "between",
}


class QuotaExceededError(RuntimeError):
    """Raised when API quota is exhausted."""


@dataclass
class UsageStats:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    estimated_cost_usd: float = 0.0

    def add(self, prompt_tokens: int, completion_tokens: int, model: str) -> None:
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        total_tokens = prompt_tokens + completion_tokens

        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.total_tokens += total_tokens
        self.estimated_cost_usd += estimate_cost_usd(
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read article content from JSON/JSONL and enrich each article with "
            "AI-generated summary and tags."
        )
    )
    parser.add_argument("--input", required=True, help="Input JSON or JSONL file path.")
    parser.add_argument("--output", required=True, help="Output JSON file path.")
    parser.add_argument(
        "--provider",
        choices=("openai", "deepseek", "hf-local", "none"),
        default=DEFAULT_PROVIDER,
        help="AI provider. Default: openai",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"OpenAI/DeepSeek model name. Default: {DEFAULT_OPENAI_MODEL}",
    )
    parser.add_argument(
        "--skill-file",
        type=Path,
        default=None,
        help="Optional skill markdown file injected into OpenAI/DeepSeek system prompt",
    )
    parser.add_argument(
        "--deepseek-base-url",
        default=os.getenv("DEEPSEEK_BASE_URL", DEEPSEEK_DEFAULT_BASE_URL),
        help="DeepSeek API base URL. Default: env DEEPSEEK_BASE_URL or https://api.deepseek.com",
    )
    parser.add_argument(
        "--hf-model",
        default="sshleifer/distilbart-cnn-12-6",
        help="Local Hugging Face model name when provider=hf-local",
    )
    parser.add_argument(
        "--max-input-chars",
        type=int,
        default=12000,
        help="Max characters of article content sent to AI. Default: 12000",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=0.3,
        help="Delay between API calls to reduce rate limit risk. Default: 0.3",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=4,
        help="Max retries for AI API network/rate-limit errors. Default: 4",
    )
    parser.add_argument(
        "--retry-base-seconds",
        type=float,
        default=1.0,
        help="Base seconds for exponential backoff. Default: 1.0",
    )
    parser.add_argument(
        "--stats-output",
        type=Path,
        default=None,
        help="Optional path to write usage/cost/failure stats JSON",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="Optional log file path",
    )
    return parser.parse_args()


def configure_logging(log_file: Path | None) -> logging.Logger:
    logger = logging.getLogger("step3_ai_extract")
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


def read_articles(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return []

    if path.suffix.lower() == ".jsonl":
        items = [json.loads(line) for line in raw.splitlines() if line.strip()]
    else:
        data = json.loads(raw)
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("articles"), list):
            items = data["articles"]
        else:
            raise ValueError(
                "Unsupported JSON shape. Expected a list or {'articles': [...]}."
            )

    normalized: list[dict[str, Any]] = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        article = dict(item)
        article.setdefault("id", i + 1)
        normalized.append(article)
    return normalized


def get_content(article: dict[str, Any]) -> str:
    for key in ("content", "body", "text", "article"):
        value = article.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_tags(text: str, limit: int = 10) -> list[str]:
    raw = TAG_SPLIT_RE.split(text)
    tags: list[str] = []
    seen: set[str] = set()
    for part in raw:
        tag = part.strip(" -#\t\r")
        if not tag:
            continue
        lower = tag.lower()
        if lower in seen:
            continue
        seen.add(lower)
        tags.append(tag)
        if len(tags) >= limit:
            break
    return tags


def parse_json_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    try:
        loaded = json.loads(stripped)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    match = JSON_BLOCK_RE.search(stripped)
    if not match:
        raise ValueError(f"Cannot parse JSON payload from response: {stripped[:200]}")
    loaded = json.loads(match.group(0))
    if not isinstance(loaded, dict):
        raise ValueError("Parsed JSON payload is not an object")
    return loaded


def extract_tags_from_content(content: str, limit: int = 10) -> list[str]:
    counts = Counter(
        token.lower()
        for token in WORD_RE.findall(content.lower())
        if token.lower() not in STOPWORDS
    )
    tags = [token for token, _ in counts.most_common(limit)]
    return tags


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    pricing = PRICING_PER_1M_TOKENS.get(model)
    if not pricing:
        return 0.0
    return (
        prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]
    ) / 1_000_000.0


def resolve_provider_model(provider: str, model: str, hf_model: str) -> str:
    if provider == "hf-local":
        return hf_model
    if provider == "none":
        return "none"
    if provider == "deepseek" and model == DEFAULT_OPENAI_MODEL:
        return DEFAULT_DEEPSEEK_MODEL
    return model


def resolve_provider_api_key(provider: str) -> str | None:
    if provider == "openai":
        return os.getenv("OPENAI_API_KEY")
    if provider == "deepseek":
        return os.getenv("DEEPSEEK_API_KEY") or os.getenv("OPENAI_API_KEY")
    return None


def resolve_deepseek_chat_url(base_url: str) -> str:
    normalized = base_url.strip() or DEEPSEEK_DEFAULT_BASE_URL
    normalized = normalized.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def resolve_skill_file_path(skill_file: Path | None) -> Path | None:
    if skill_file is None:
        return None
    candidate = skill_file.expanduser()
    if not candidate.is_absolute():
        return (Path.cwd() / candidate).resolve()
    return candidate


def load_skill_instructions(skill_file: Path | None) -> tuple[str, Path | None]:
    resolved = resolve_skill_file_path(skill_file)
    if resolved is None:
        return "", None
    if not resolved.exists():
        raise FileNotFoundError(f"Skill file not found: {resolved}")
    if not resolved.is_file():
        raise RuntimeError(f"Skill path is not a file: {resolved}")
    return resolved.read_text(encoding="utf-8").strip(), resolved


def build_system_prompt(skill_instructions: str) -> str:
    if not skill_instructions:
        return BASE_SYSTEM_PROMPT
    return (
        f"{BASE_SYSTEM_PROMPT}\n\n"
        "Follow the additional SKILL instructions below while preserving output schema.\n"
        "[SKILL INSTRUCTIONS BEGIN]\n"
        f"{skill_instructions}\n"
        "[SKILL INSTRUCTIONS END]"
    )


def _backoff_seconds(attempt: int, base_seconds: float) -> float:
    jitter = random.uniform(0.0, 0.2)
    return base_seconds * (2 ** (attempt - 1)) + jitter


def call_chat_completions_with_retries(
    *,
    api_url: str,
    provider_name: str,
    api_key: str,
    model: str,
    system_prompt: str,
    content: str,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> tuple[str, list[str], int, int]:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": (
                    "Read the following article and return strict JSON with exactly two keys:\n"
                    "summary: concise factual summary around 100 words.\n"
                    "tags: 5-10 tags as a JSON array of short strings.\n\n"
                    f"{content}"
                ),
            },
        ],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                response_data = json.loads(resp.read().decode("utf-8"))
                try:
                    content_text = response_data["choices"][0]["message"]["content"].strip()
                except (KeyError, IndexError, AttributeError) as err:
                    raise RuntimeError(f"Unexpected {provider_name} response: {response_data}") from err

                parsed_payload = parse_json_payload(content_text)
                summary = str(parsed_payload.get("summary") or "").strip()
                raw_tags = parsed_payload.get("tags")
                if isinstance(raw_tags, list):
                    tags = parse_tags(", ".join(str(item) for item in raw_tags))
                else:
                    tags = parse_tags(str(raw_tags or ""))

                usage = response_data.get("usage") if isinstance(response_data, dict) else {}
                prompt_tokens = int((usage or {}).get("prompt_tokens", 0))
                completion_tokens = int((usage or {}).get("completion_tokens", 0))

                if not summary:
                    summary = content[:500].strip()
                if not tags:
                    tags = extract_tags_from_content(content, limit=10)
                return summary, tags[:10], prompt_tokens, completion_tokens
        except urllib.error.HTTPError as err:
            details = err.read().decode("utf-8", errors="ignore")
            lowered = details.lower()
            if (
                "insufficient_quota" in lowered
                or "insufficient balance" in lowered
                or "余额不足" in details
            ):
                raise QuotaExceededError(
                    f"{provider_name} quota exceeded. Please check billing/quota. details={details}"
                ) from err

            if err.code == 400 and "response_format" in lowered:
                logger.warning(
                    "%s response_format unsupported for model=%s, retrying without response_format",
                    provider_name,
                    model,
                )
                payload.pop("response_format", None)
                last_error = err
                continue

            if err.code in (408, 429, 500, 502, 503, 504):
                wait_seconds = _backoff_seconds(attempt, retry_base_seconds)
                logger.warning(
                    "%s API transient error (attempt %s/%s): HTTP %s, retrying in %.2fs",
                    provider_name,
                    attempt,
                    max_retries,
                    err.code,
                    wait_seconds,
                )
                time.sleep(wait_seconds)
                last_error = err
                continue
            raise RuntimeError(
                f"{provider_name} API request failed: HTTP {err.code} - {details}"
            ) from err
        except urllib.error.URLError as err:
            wait_seconds = _backoff_seconds(attempt, retry_base_seconds)
            logger.warning(
                "%s API network error (attempt %s/%s): %s, retrying in %.2fs",
                provider_name,
                attempt,
                max_retries,
                err,
                wait_seconds,
            )
            time.sleep(wait_seconds)
            last_error = err

    raise RuntimeError(f"{provider_name} API failed after {max_retries} attempts: {last_error}")


def generate_with_openai(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    content: str,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> tuple[str, list[str], int, int]:
    return call_chat_completions_with_retries(
        api_url=OPENAI_CHAT_COMPLETIONS_URL,
        provider_name="OpenAI",
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        content=content,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        logger=logger,
    )


def generate_with_deepseek(
    *,
    api_key: str,
    base_url: str,
    model: str,
    system_prompt: str,
    content: str,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> tuple[str, list[str], int, int]:
    return call_chat_completions_with_retries(
        api_url=resolve_deepseek_chat_url(base_url),
        provider_name="DeepSeek",
        api_key=api_key,
        model=model,
        system_prompt=system_prompt,
        content=content,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
        logger=logger,
    )

def build_hf_summarizer(model_name: str):
    try:
        from transformers import pipeline  # type: ignore
    except ImportError as err:
        raise RuntimeError(
            "provider=hf-local requires transformers (and a backend like torch). "
            "Install with: pip install transformers torch"
        ) from err
    return pipeline("summarization", model=model_name)


def generate_with_hf_local(content: str, summarizer: Any) -> tuple[str, list[str]]:
    result = summarizer(
        content[:4000],
        max_length=130,
        min_length=40,
        do_sample=False,
    )
    summary = ""
    if isinstance(result, list) and result and isinstance(result[0], dict):
        summary = str(result[0].get("summary_text", "")).strip()
    tags = extract_tags_from_content(content, limit=10)
    return summary, tags


def ensure_stats_output_path(output_path: Path, custom_path: Path | None) -> Path:
    if custom_path is not None:
        return custom_path
    return output_path.with_name(f"{output_path.stem}.stats.json")


def write_stats(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def enrich_articles(
    *,
    articles: list[dict[str, Any]],
    provider: str,
    api_key: str | None,
    model: str,
    hf_model: str,
    system_prompt: str,
    deepseek_base_url: str,
    max_input_chars: int,
    sleep_seconds: float,
    max_retries: int,
    retry_base_seconds: float,
    logger: logging.Logger,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    total = len(articles)
    usage = UsageStats()
    quota_exhausted = False
    processed = 0
    success = 0
    failed = 0
    skipped = 0

    hf_summarizer = None
    if provider == "hf-local":
        hf_summarizer = build_hf_summarizer(hf_model)

    for index, article in enumerate(articles, start=1):
        content = get_content(article)
        if not content:
            logger.warning(
                "[%s/%s] article id=%s skipped: empty content",
                index,
                total,
                article.get("id"),
            )
            enriched.append({**article, "summary": "", "tags": [], "ai_error": "empty_content"})
            skipped += 1
            continue

        clipped_content = content[:max_input_chars]
        logger.info("[%s/%s] article id=%s processing", index, total, article.get("id"))
        processed += 1

        try:
            if provider == "none":
                summary, tags = "", []
                prompt_tokens = 0
                completion_tokens = 0
            elif provider == "hf-local":
                summary, tags = generate_with_hf_local(clipped_content, hf_summarizer)
                prompt_tokens = 0
                completion_tokens = 0
            elif provider == "deepseek":
                if not api_key:
                    raise RuntimeError("Missing DEEPSEEK_API_KEY environment variable.")
                summary, tags, prompt_tokens, completion_tokens = generate_with_deepseek(
                    api_key=api_key,
                    base_url=deepseek_base_url,
                    model=model,
                    system_prompt=system_prompt,
                    content=clipped_content,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    logger=logger,
                )
                usage.add(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                )
            else:
                if not api_key:
                    raise RuntimeError("Missing OPENAI_API_KEY environment variable.")
                summary, tags, prompt_tokens, completion_tokens = generate_with_openai(
                    api_key=api_key,
                    model=model,
                    system_prompt=system_prompt,
                    content=clipped_content,
                    max_retries=max_retries,
                    retry_base_seconds=retry_base_seconds,
                    logger=logger,
                )
                usage.add(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    model=model,
                )

            enriched.append({**article, "summary": summary, "tags": tags[:10]})
            success += 1
            logger.info(
                "[%s/%s] article id=%s done (%s tags)",
                index,
                total,
                article.get("id"),
                len(tags),
            )
        except QuotaExceededError as err:
            failed += 1
            quota_exhausted = True
            logger.error(
                "[%s/%s] article id=%s quota exhausted: %s",
                index,
                total,
                article.get("id"),
                err,
            )
            enriched.append(
                {**article, "summary": "", "tags": [], "ai_error": "quota_exceeded"}
            )
            break
        except Exception as err:  # noqa: BLE001
            failed += 1
            logger.exception(
                "[%s/%s] article id=%s failed: %s",
                index,
                total,
                article.get("id"),
                err,
            )
            enriched.append(
                {
                    **article,
                    "summary": "",
                    "tags": extract_tags_from_content(clipped_content, limit=10),
                    "ai_error": str(err),
                }
            )

        if sleep_seconds > 0:
            time.sleep(sleep_seconds)

    if quota_exhausted and len(enriched) < total:
        for article in articles[len(enriched) :]:
            enriched.append(
                {**article, "summary": "", "tags": [], "ai_error": "quota_exceeded"}
            )
            failed += 1

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": resolve_provider_model(provider, model, hf_model),
        "total_articles": total,
        "processed_articles": processed,
        "success_articles": success,
        "failed_articles": failed,
        "skipped_articles": skipped,
        "quota_exhausted": quota_exhausted,
        "usage": usage.as_dict(),
    }
    return enriched, stats


def main() -> int:
    args = parse_args()
    logger = configure_logging(args.log_file)
    output_path = Path(args.output)
    stats_path = ensure_stats_output_path(output_path, args.stats_output)
    resolved_model = resolve_provider_model(args.provider, args.model, args.hf_model)
    try:
        skill_instructions, resolved_skill_file = load_skill_instructions(args.skill_file)
    except Exception as err:  # noqa: BLE001
        logger.error("Failed to load --skill-file: %s", err)
        return 1
    system_prompt = build_system_prompt(skill_instructions)
    if resolved_skill_file is not None:
        logger.info(
            "Loaded skill instructions from %s (%s chars)",
            resolved_skill_file,
            len(skill_instructions),
        )

    input_path = Path(args.input)
    if not input_path.exists():
        logger.error("Input file not found: %s", input_path)
        return 1

    if args.provider in ("openai", "deepseek"):
        api_key = resolve_provider_api_key(args.provider)
        if not api_key:
            if args.provider == "openai":
                logger.error("Missing OPENAI_API_KEY environment variable.")
            else:
                logger.error("Missing DEEPSEEK_API_KEY environment variable.")
            return 1
    else:
        api_key = None

    try:
        articles = read_articles(input_path)
    except Exception as err:  # noqa: BLE001
        logger.exception("Failed to parse input: %s", err)
        return 1

    if not articles:
        logger.warning("No articles found in input. Writing empty output.")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("[]\n", encoding="utf-8")
        write_stats(
            stats_path,
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "provider": args.provider,
                "model": resolved_model,
                "total_articles": 0,
                "processed_articles": 0,
                "success_articles": 0,
                "failed_articles": 0,
                "skipped_articles": 0,
                "quota_exhausted": False,
                "usage": UsageStats().as_dict(),
            },
        )
        logger.info("Wrote empty output to %s and stats to %s", output_path, stats_path)
        return 0

    try:
        enriched, stats = enrich_articles(
            articles=articles,
            provider=args.provider,
            api_key=api_key,
            model=resolved_model,
            hf_model=args.hf_model,
            system_prompt=system_prompt,
            deepseek_base_url=args.deepseek_base_url,
            max_input_chars=args.max_input_chars,
            sleep_seconds=args.sleep_seconds,
            max_retries=args.max_retries,
            retry_base_seconds=args.retry_base_seconds,
            logger=logger,
        )
    except Exception as err:  # noqa: BLE001
        logger.exception("Processing failed: %s", err)
        return 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(enriched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Wrote %s articles to %s", len(enriched), output_path)

    write_stats(stats_path, stats)
    logger.info(
        "Stats written to %s (total_tokens=%s estimated_cost_usd=%s)",
        stats_path,
        stats["usage"]["total_tokens"],
        stats["usage"]["estimated_cost_usd"],
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
