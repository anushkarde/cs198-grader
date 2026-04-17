#!/usr/bin/env python3
"""
Apply LLM-produced scores + comment to a Paperless grading page via Playwright.
Supports --dry-run (no DOM writes), --review (pause after fill), --save (click save).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Page

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    fill_save_button_selector,
    load_config,
    playwright_browser,
    storage_state_path,
)
from src.playwright_util import launch_browser, new_context_with_storage  # noqa: E402


def _j(s: str) -> str:
    """Quote a string for use in a CSS attribute selector."""
    return json.dumps(s)


def _fill_scalar_input(page: Page, field: dict[str, Any], value: Any, actions: list[str], dry_run: bool) -> None:
    dom = field.get("dom") or {}
    ctl = (field.get("control_type") or "").lower()
    tag = (dom.get("tag") or "").lower()
    typ = (dom.get("type") or "").lower()
    name = dom.get("name")
    fid = dom.get("id")

    if dry_run:
        actions.append(f"set {field.get('key')!r} ({ctl}) = {value!r}")
        return

    if ctl == "select" or tag == "select":
        if fid:
            loc = page.locator(f"select[id={_j(str(fid))}]").first
        elif name:
            loc = page.locator(f"select[name={_j(str(name))}]").first
        else:
            raise ValueError(f"select field {field.get('key')!r} has no id or name in dom snapshot")
        loc.select_option(str(value))
        actions.append(f"select {field.get('key')!r} = {value!r}")
        return

    if fid:
        loc = page.locator(f"[id={_j(str(fid))}]").first
    elif name:
        loc = page.locator(f"[name={_j(str(name))}]").first
    else:
        raise ValueError(f"field {field.get('key')!r} has no id or name in dom snapshot")

    if typ == "checkbox":
        want = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
        loc.set_checked(want)
        actions.append(f"checkbox {field.get('key')!r} = {want}")
        return

    if tag == "textarea" or typ == "textarea":
        loc.fill(str(value))
        actions.append(f"textarea {field.get('key')!r} (len={len(str(value))})")
        return

    # number, text, etc.
    loc.fill(str(value))
    actions.append(f"fill {field.get('key')!r} = {value!r}")


def _fill_radio_group(page: Page, field: dict[str, Any], value: Any, actions: list[str], dry_run: bool) -> None:
    dom = field.get("dom") or {}
    name = dom.get("name")
    if not name:
        raise ValueError(f"radio_group {field.get('key')!r} missing name")
    val = str(value)
    if dry_run:
        actions.append(f"radio {field.get('key')!r} name={name!r} value={val!r}")
        return
    sel = f'input[type="radio"][name={_j(str(name))}][value={_j(val)}]'
    page.locator(sel).first.check()
    actions.append(f"radio {field.get('key')!r} checked value={val!r}")


def apply_grades_to_page(
    page: Page,
    schema: dict[str, Any],
    grades: dict[str, Any],
    *,
    dry_run: bool,
) -> list[str]:
    """
    Fill the grading form. `grades` must be {"scores": {...}, "comment": "..."}.
    Returns a log of actions.
    """
    actions: list[str] = []
    scores = grades.get("scores") if isinstance(grades.get("scores"), dict) else {}
    comment = grades.get("comment")
    if comment is None:
        comment = ""
    comment = str(comment)

    fields = schema.get("fields") or []
    comment_field = schema.get("comment_field") or {}
    comment_key = None
    if isinstance(comment_field, dict):
        comment_key = comment_field.get("key")

    for field in fields:
        if not isinstance(field, dict):
            continue
        fk = field.get("key")
        if not fk:
            continue
        ctl = (field.get("control_type") or "").lower()
        role = field.get("role") or "rubric"

        if role == "comment" or fk == comment_key:
            _fill_scalar_input(page, field, comment, actions, dry_run)
            continue

        if fk not in scores:
            actions.append(f"skip {fk!r} (no score in grades)")
            continue
        val = scores[fk]

        if ctl == "radio_group":
            _fill_radio_group(page, field, val, actions, dry_run)
        else:
            _fill_scalar_input(page, field, val, actions, dry_run)

    return actions


def click_save_if_configured(page: Page, selector: str, *, dry_run: bool, actions: list[str]) -> None:
    if dry_run:
        actions.append(f"would click save matching {selector!r}")
        return
    loc = page.locator(selector).first
    loc.click(timeout=30_000)
    actions.append("clicked save control")


def fill_grading_session(
    url: str,
    schema: dict[str, Any],
    grades: dict[str, Any],
    *,
    storage_state: Path,
    browser_name: str,
    headless: bool,
    dry_run: bool,
    review: bool,
    save: bool,
    save_selector: str,
    wait_ms: int,
) -> list[str]:
    from playwright.sync_api import sync_playwright

    actions: list[str] = []
    with sync_playwright() as p:
        browser = launch_browser(p, browser_name, headless=headless)
        try:
            context = new_context_with_storage(browser, storage_state)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=120_000)
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
            sub = apply_grades_to_page(page, schema, grades, dry_run=dry_run)
            actions.extend(sub)
            if save:
                click_save_if_configured(page, save_selector, dry_run=dry_run, actions=actions)
            if review:
                print(
                    "\n--review-- Inspect the browser window. Press Enter to close.",
                    file=sys.stderr,
                    flush=True,
                )
                input()
        finally:
            browser.close()
    return actions


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Fill a Paperless grading page from grades JSON.")
    p.add_argument("--config", type=Path, default=None, help="config.yaml path.")
    p.add_argument("--url", required=True, help="Grading page URL.")
    p.add_argument(
        "--schema",
        type=Path,
        required=True,
        help="LLM rubric schema JSON (from scrape_grading_form).",
    )
    p.add_argument(
        "--grades",
        type=Path,
        required=True,
        help='JSON with {"scores": {...}, "comment": "..."}.',
    )
    p.add_argument(
        "--storage",
        type=Path,
        default=None,
        help="storage_state.json (default: config).",
    )
    p.add_argument("--headed", action="store_true", help="Show browser (default: headless).")
    p.add_argument("--wait-ms", type=int, default=2000, help="Wait after navigation for SPAs.")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print actions only; do not change inputs or click save.",
    )
    p.add_argument(
        "--review",
        action="store_true",
        help="After fill, wait for Enter before closing the browser.",
    )
    p.add_argument(
        "--save",
        action="store_true",
        help="Click the save/submit control after filling (use with care).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)

    schema = json.loads(Path(args.schema).read_text(encoding="utf-8"))
    grades = json.loads(Path(args.grades).read_text(encoding="utf-8"))
    if not isinstance(schema, dict) or not isinstance(grades, dict):
        print("schema and grades must be JSON objects.", file=sys.stderr)
        return 2

    state_path = args.storage or storage_state_path(cfg)
    if not state_path.is_file():
        print(f"Missing storage state: {state_path}", file=sys.stderr)
        return 2

    actions = fill_grading_session(
        args.url.strip(),
        schema,
        grades,
        storage_state=state_path,
        browser_name=playwright_browser(cfg),
        headless=not args.headed,
        dry_run=args.dry_run,
        review=args.review,
        save=args.save,
        save_selector=fill_save_button_selector(cfg),
        wait_ms=args.wait_ms,
    )
    for line in actions:
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
