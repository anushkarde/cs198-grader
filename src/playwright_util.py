"""Shared Playwright browser/context helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.sync_api import Browser, Playwright, sync_playwright


def browser_factory(playwright: Playwright, name: str):
    n = (name or "chromium").lower()
    if n == "chromium":
        return playwright.chromium
    if n == "firefox":
        return playwright.firefox
    if n == "webkit":
        return playwright.webkit
    raise ValueError(f"Unknown browser: {name!r} (use chromium, firefox, webkit)")


def launch_browser(playwright: Playwright, name: str, *, headless: bool) -> Browser:
    return browser_factory(playwright, name).launch(headless=headless)


def new_context_with_storage(
    browser: Browser,
    storage_state: Path | None,
) -> Any:
    kwargs: dict[str, Any] = {}
    if storage_state is not None and storage_state.is_file():
        kwargs["storage_state"] = str(storage_state)
    return browser.new_context(**kwargs)
