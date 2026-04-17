#!/usr/bin/env python3
"""
Open Paperless in a headed browser, wait until you finish logging in, then save
Playwright storage_state JSON for non-interactive reuse (probe, scrapers, fill).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

# Allow `python src/login_session.py` without installing the package
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import load_config, playwright_browser, storage_state_path  # noqa: E402
from src.playwright_util import launch_browser  # noqa: E402


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Save Paperless login to storage_state JSON.")
    p.add_argument(
        "--start-url",
        default="https://cs198.stanford.edu/paperless/",
        help="Where to open first (default: CS198 Paperless root).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ./config.yaml).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Override output path (default: playwright.storage_state_path in config).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    out = args.output or storage_state_path(cfg)
    browser_name = playwright_browser(cfg)

    print(
        "Opening a browser window. Log in to Stanford / CS198 Paperless.\n"
        "When you are fully logged in (dashboard or assignment pages load), "
        "return to this terminal and press Enter to save the session.\n",
        flush=True,
    )

    with sync_playwright() as p:
        browser = launch_browser(p, browser_name, headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(args.start_url, wait_until="domcontentloaded", timeout=120_000)
            input("Press Enter here after you are logged in... ")
            out.parent.mkdir(parents=True, exist_ok=True)
            context.storage_state(path=str(out))
        finally:
            browser.close()

    print(f"Saved storage state to {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
