#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FEEDS_FILE = BASE_DIR / "feeds.txt"
DEFAULT_OUTPUT_DIR = BASE_DIR / "output"
DEFAULT_LOG_DIR = BASE_DIR / "logs"
DEFAULT_DB_PATH = BASE_DIR / "data" / "history.db"
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fetch + AI + feed generation daily pipeline")
    parser.add_argument("--feeds-file", type=Path, default=DEFAULT_FEEDS_FILE, help="Feeds list file")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="Output directory")
    parser.add_argument(
        "--date",
        dest="target_date",
        default=None,
        help="Target date in YYYY-MM-DD (default: UTC today)",
    )
    parser.add_argument(
        "--provider",
        choices=("openai", "deepseek", "hf-local", "none"),
        default="openai",
        help="AI provider for step 3",
    )
    parser.add_argument("--model", default=DEFAULT_OPENAI_MODEL, help="OpenAI/DeepSeek model")
    parser.add_argument(
        "--deepseek-base-url",
        default=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL),
        help="DeepSeek API base URL",
    )
    parser.add_argument("--hf-model", default="sshleifer/distilbart-cnn-12-6", help="HF model name")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="SQLite path for dedupe")
    parser.add_argument("--timeout", type=int, default=20, help="Fetch HTTP timeout")
    parser.add_argument("--max-retries", type=int, default=3, help="Retry count for fetch/OpenAI API")
    parser.add_argument("--sleep-seconds", type=float, default=0.3, help="Sleep between AI calls")
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR, help="Log directory")
    parser.add_argument("--feed-title", default="Daily Tech Posts", help="Output JSON Feed title")
    parser.add_argument("--home-page-url", default="", help="Output JSON Feed home_page_url")
    parser.add_argument("--feed-url", default="", help="Output JSON Feed feed_url")
    return parser.parse_args()


def configure_logger(log_file: Path) -> logging.Logger:
    logger = logging.getLogger("run_daily_pipeline")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def run_step(cmd: list[str], logger: logging.Logger) -> None:
    logger.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.stdout.strip():
        logger.info("stdout: %s", result.stdout.strip())
    if result.stderr.strip():
        logger.info("stderr: %s", result.stderr.strip())
    if result.returncode != 0:
        raise RuntimeError(f"Command failed (exit {result.returncode}): {' '.join(cmd)}")


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if isinstance(parsed, dict):
        return parsed
    return {}


def resolve_target_date(raw_target_date: str | None) -> str:
    if raw_target_date:
        datetime.strptime(raw_target_date, "%Y-%m-%d")
        return raw_target_date
    return datetime.now(timezone.utc).date().isoformat()


def resolve_provider_model(provider: str, model: str, hf_model: str) -> str:
    if provider == "hf-local":
        return hf_model
    if provider == "none":
        return "none"
    if provider == "deepseek" and model == DEFAULT_OPENAI_MODEL:
        return DEFAULT_DEEPSEEK_MODEL
    return model


def main() -> int:
    args = parse_args()
    try:
        target_date = resolve_target_date(args.target_date)
    except ValueError:
        print(f"Invalid --date value: {args.target_date}. Expected YYYY-MM-DD.", file=sys.stderr)
        return 1

    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    pipeline_log = args.log_dir / f"{target_date}.pipeline.log"
    logger = configure_logger(pipeline_log)

    fetch_output = args.output_dir / f"{target_date}.json"
    fetch_stats = args.output_dir / f"{target_date}.fetch.stats.json"
    ai_output = args.output_dir / f"{target_date}.enriched.json"
    ai_stats = args.output_dir / f"{target_date}.ai.stats.json"
    feed_output = args.output_dir / f"{target_date}.feed.json"
    summary_output = args.output_dir / f"{target_date}.pipeline.stats.json"

    scripts_dir = Path(__file__).resolve().parent
    fetch_script = scripts_dir / "fetch_rss_today.py"
    ai_script = scripts_dir / "step3_ai_extract.py"
    feed_script = scripts_dir / "generate_json_feed.py"

    fetch_cmd = [
        sys.executable,
        str(fetch_script),
        "--feeds-file",
        str(args.feeds_file),
        "--output-dir",
        str(args.output_dir),
        "--date",
        target_date,
        "--timeout",
        str(args.timeout),
        "--max-retries",
        str(args.max_retries),
        "--stats-output",
        str(fetch_stats),
        "--log-file",
        str(args.log_dir / f"{target_date}.fetch.log"),
    ]
    if args.db_path:
        fetch_cmd.extend(["--db-path", str(args.db_path)])

    ai_cmd = [
        sys.executable,
        str(ai_script),
        "--input",
        str(fetch_output),
        "--output",
        str(ai_output),
        "--provider",
        args.provider,
        "--model",
        resolve_provider_model(args.provider, args.model, args.hf_model),
        "--deepseek-base-url",
        args.deepseek_base_url,
        "--hf-model",
        args.hf_model,
        "--max-retries",
        str(args.max_retries),
        "--sleep-seconds",
        str(args.sleep_seconds),
        "--stats-output",
        str(ai_stats),
        "--log-file",
        str(args.log_dir / f"{target_date}.ai.log"),
    ]

    feed_cmd = [
        sys.executable,
        str(feed_script),
        "--input",
        str(ai_output),
        "--output",
        str(feed_output),
        "--title",
        args.feed_title,
    ]
    if args.home_page_url:
        feed_cmd.extend(["--home-page-url", args.home_page_url])
    if args.feed_url:
        feed_cmd.extend(["--feed-url", args.feed_url])

    try:
        run_step(fetch_cmd, logger)
        run_step(ai_cmd, logger)
        run_step(feed_cmd, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Pipeline failed: %s", exc)
        return 1

    fetch_stats_payload = read_json(fetch_stats)
    ai_stats_payload = read_json(ai_stats)
    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_date": target_date,
        "provider": args.provider,
        "model": resolve_provider_model(args.provider, args.model, args.hf_model),
        "outputs": {
            "fetch": str(fetch_output),
            "enriched": str(ai_output),
            "feed": str(feed_output),
            "fetch_stats": str(fetch_stats),
            "ai_stats": str(ai_stats),
            "pipeline_log": str(pipeline_log),
        },
        "fetch": fetch_stats_payload,
        "ai": ai_stats_payload,
        "estimated_cost_usd": (ai_stats_payload.get("usage") or {}).get("estimated_cost_usd", 0),
    }
    summary_output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    logger.info("Pipeline completed. Summary written to %s", summary_output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
