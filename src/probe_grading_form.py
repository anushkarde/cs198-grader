#!/usr/bin/env python3
"""
Load saved Playwright storage_state, open one grading URL, and list form fields
(inputs, selects, textareas) for rubric calibration.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    default_probe_grading_url,
    load_config,
    playwright_browser,
    storage_state_path,
)
from src.form_field_probe import (  # noqa: E402
    collect_form_fields_json,
    fields_to_json_text,
    format_fields_human_readable,
)
from src.scrape_grading_form import build_llm_schema, schema_to_json_text  # noqa: E402
from src.playwright_util import launch_browser, new_context_with_storage  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Probe a Paperless grading page and list form fields (requires login session).",
    )
    p.add_argument(
        "--url",
        default="",
        help="Grading page URL (default: paperless.probe_grading_url in config or CS198_PROBE_GRADING_URL).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ./config.yaml).",
    )
    p.add_argument(
        "--storage",
        type=Path,
        default=None,
        help="storage_state.json from login_session.py (default: config / CS198_STORAGE_STATE).",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Show the browser window (default: headless).",
    )
    p.add_argument(
        "--wait-ms",
        type=int,
        default=2000,
        help="Extra wait after navigation for SPAs to render (default: 2000).",
    )
    p.add_argument(
        "--format",
        choices=("human", "json", "schema"),
        default="human",
        help="human = readable list; json = raw DOM probe; schema = LLM rubric JSON (labels, types, max_points).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    url = (args.url or "").strip() or default_probe_grading_url(cfg)
    if not url:
        print(
            "No URL provided. Set paperless.probe_grading_url in config.yaml, "
            "or CS198_PROBE_GRADING_URL, or pass --url.",
            file=sys.stderr,
        )
        return 2

    state_path = args.storage or storage_state_path(cfg)
    if not state_path.is_file():
        print(
            f"Missing storage state file: {state_path}\n"
            "Run: python3 src/login_session.py\n",
            file=sys.stderr,
        )
        return 2

    browser_name = playwright_browser(cfg)
    headless = not args.headed

    with sync_playwright() as p:
        browser = launch_browser(p, browser_name, headless=headless)
        try:
            context = new_context_with_storage(browser, state_path)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            if args.wait_ms > 0:
                time.sleep(args.wait_ms / 1000.0)
            fields = collect_form_fields_json(page)
        finally:
            browser.close()

    if args.format == "json":
        sys.stdout.write(fields_to_json_text(fields))
        print(f"Listed {len(fields)} field(s).", file=sys.stderr)
    elif args.format == "schema":
        schema = build_llm_schema(fields, source_url=url)
        sys.stdout.write(schema_to_json_text(schema))
        n = len(schema.get("fields") or [])
        print(f"Serialized {n} schema field(s).", file=sys.stderr)
    else:
        sys.stdout.write(format_fields_human_readable(fields))
        print(f"Listed {len(fields)} field(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
