#!/usr/bin/env python3
"""
Scrape the Paperless section assignment page to map folder keys (SUNet or
submission id) to each student's submission grading URL. Optionally merge or
override with a hand-edited students.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    default_section_assignment_url,
    discover_folder_key,
    discover_students_json_path,
    load_config,
    playwright_browser,
    storage_state_path,
)
from src.playwright_util import launch_browser, new_context_with_storage  # noqa: E402

# e.g. .../submission/1451/374057?fromSection=8004
_SUBMISSION_PATH_RE = re.compile(r"/submission/(\d+)/(\d+)(?:\?|$|/)", re.IGNORECASE)

# Collect <a href*="/submission/"> rows; dedupe by canonical submission URL.
_LINK_SCANNER_JS = """
() => {
  const anchors = Array.from(document.querySelectorAll('a[href*="/submission/"]'));
  const seenHref = new Set();
  const out = [];
  for (const a of anchors) {
    const href = a.href || "";
    if (!href.includes("/submission/")) continue;
    if (seenHref.has(href)) continue;
    seenHref.add(href);
    const tr = a.closest("tr");
    const rowText = tr ? tr.innerText.replace(/\\s+/g, " ").trim() : "";
    const linkText = (a.textContent || "").replace(/\\s+/g, " ").trim();
    out.push({ href, rowText, linkText });
  }
  return out;
}
"""

# Words that sometimes appear in table rows but are not SUNets.
_SUNET_STOPWORDS = frozenset(
    """
    the and for not but all can has her was one our out day get him his how its may new
    now old see two way who boy did let put say she too use any are ask did get got had
    has her him his how its let may not now off old one our out own ran saw say see set
    she sit six ten too try two use was way who why yes yet you zip sub lab sec row
    view open link edit grade graded submission assign section paperless cs106a cs198
    """.split()
)


def parse_submission_url(href: str) -> tuple[str, str, str] | None:
    """Return (assignment_id, submission_id, normalized_href) if href is a submission link."""
    if not href or "/submission/" not in href:
        return None
    m = _SUBMISSION_PATH_RE.search(href)
    if not m:
        return None
    aid, sid = m.group(1), m.group(2)
    parsed = urlparse(href)
    # Strip fragment; keep query (e.g. fromSection).
    normalized = parsed._replace(fragment="").geturl()
    return aid, sid, normalized


def extract_sunet(row_text: str, link_text: str) -> str | None:
    """Best-effort SUNet from visible row text (email, then token heuristic)."""
    blob = f"{row_text} {link_text}".strip()
    if not blob:
        return None
    m = re.search(r"\b([A-Za-z0-9._-]+)@stanford\.edu\b", blob, re.IGNORECASE)
    if m:
        local = m.group(1).split("@")[0].lower()
        if re.fullmatch(r"[a-z][a-z0-9]{2,15}", local):
            return local
    for token in re.findall(r"\b[a-z][a-z0-9]{2,15}\b", blob.lower()):
        if token in _SUNET_STOPWORDS:
            continue
        if 3 <= len(token) <= 15:
            return token
    return None


def rows_to_mapping(
    rows: list[dict[str, str]],
    folder_key: str,
) -> tuple[dict[str, str], list[str]]:
    """
    Build folder_key -> submission URL. Returns (mapping, warnings).
    folder_key: 'sunet' | 'submission_id'
    """
    mapping: dict[str, str] = {}
    warnings: list[str] = []
    key_norm = (folder_key or "submission_id").strip().lower()
    if key_norm not in ("sunet", "submission_id"):
        raise ValueError(f"folder_key must be 'sunet' or 'submission_id', got {folder_key!r}")

    for i, row in enumerate(rows):
        href = (row.get("href") or "").strip()
        parsed = parse_submission_url(href)
        if not parsed:
            warnings.append(f"Row {i}: skip non-submission or unparseable href: {href!r}")
            continue
        _aid, sid, normalized = parsed
        row_text = row.get("rowText") or ""
        link_text = row.get("linkText") or ""

        if key_norm == "submission_id":
            k = sid
        else:
            sunet = extract_sunet(row_text, link_text)
            if not sunet:
                warnings.append(
                    f"Row {i}: could not infer SUNet; using submission_id {sid} as key. "
                    f"rowText={row_text[:120]!r}"
                )
                k = sid
            else:
                k = sunet

        if k in mapping and mapping[k] != normalized:
            warnings.append(
                f"Duplicate key {k!r}: keeping first URL; also saw {normalized}"
            )
            continue
        if k not in mapping:
            mapping[k] = normalized

    return mapping, warnings


def load_mapping_json(path: Path) -> dict[str, str]:
    """
    Load a JSON file produced by discover_submissions (--output) or a plain object
    of folder_key -> submission_url.
    """
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    raw = data.get("mapping") if "mapping" in data else data
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if str(k).startswith("_"):
            continue
        if isinstance(v, str) and v.strip():
            out[str(k).strip()] = v.strip()
    return out


def load_students_json(path: Path) -> dict[str, str]:
    """
    Load optional hand mapping: folder name / key -> submission URL.

    Supported shapes:
      { "jdoe": "https://...", "jdoe2": "..." }
      { "mappings": { "jdoe": "https://..." } }
    Values must be strings (URLs). Other keys are ignored.
    """
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {}
    if "mappings" in data and isinstance(data["mappings"], dict):
        raw = data["mappings"]
    else:
        raw = data
    out: dict[str, str] = {}
    for k, v in raw.items():
        if k == "mappings" or str(k).startswith("_"):
            continue
        if isinstance(v, str) and v.strip():
            out[str(k).strip()] = v.strip()
    return out


def merge_mappings(
    scraped: dict[str, str],
    hand: dict[str, str],
    *,
    hand_overrides: bool,
) -> dict[str, str]:
    """Merge scraped map with students.json. If hand_overrides, hand wins on key clash."""
    if not hand:
        return dict(scraped)
    if not hand_overrides:
        merged = dict(hand)
        merged.update(scraped)
        return merged
    merged = dict(scraped)
    merged.update(hand)
    return merged


def discover_mapping_from_page(
    *,
    section_url: str,
    storage_state: Path,
    browser_name: str,
    headless: bool,
    folder_key: str,
    wait_ms: int,
) -> tuple[dict[str, str], list[str]]:
    """Open section assignment URL with saved session and return mapping + warnings."""
    from playwright.sync_api import sync_playwright

    rows: list[dict[str, str]] = []
    with sync_playwright() as p:
        browser = launch_browser(p, browser_name, headless=headless)
        try:
            context = new_context_with_storage(browser, storage_state)
            page = context.new_page()
            page.goto(section_url, wait_until="domcontentloaded", timeout=120_000)
            if wait_ms > 0:
                time.sleep(wait_ms / 1000.0)
            raw = page.evaluate(_LINK_SCANNER_JS)
            if isinstance(raw, list):
                for item in raw:
                    if isinstance(item, dict):
                        rows.append(
                            {
                                "href": str(item.get("href") or ""),
                                "rowText": str(item.get("rowText") or ""),
                                "linkText": str(item.get("linkText") or ""),
                            }
                        )
        finally:
            browser.close()

    mapping, warnings = rows_to_mapping(rows, folder_key)
    if not rows:
        warnings.append(
            "No links matching a[href*='/submission/'] were found. "
            "Check the assignment URL and DOM, or use students.json."
        )
    return mapping, warnings


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Map folder keys (SUNet or submission id) to Paperless submission URLs.",
    )
    p.add_argument(
        "--url",
        default="",
        help="Section assignment page URL (default: paperless.section_assignment_url or CS198_SECTION_ASSIGNMENT_URL).",
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
        help="Extra wait after navigation for SPAs (default: 2000).",
    )
    p.add_argument(
        "--folder-key",
        choices=("sunet", "submission_id"),
        default=None,
        help="Map folder names to URLs by SUNet or submission id (default: discover.folder_key in config).",
    )
    p.add_argument(
        "--students-json",
        type=Path,
        default=None,
        help="Optional JSON file: folder_key -> submission URL (default: discover.students_json_path or CS198_STUDENTS_JSON).",
    )
    p.add_argument(
        "--no-students-json",
        action="store_true",
        help="Do not load students.json even if configured.",
    )
    p.add_argument(
        "--no-hand-override",
        dest="hand_override",
        action="store_false",
        default=True,
        help="When keys conflict, prefer scraped URLs over students.json (default: students.json wins).",
    )
    p.add_argument(
        "--skip-scrape",
        action="store_true",
        help="Do not load the section page; build mapping from students.json only.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write JSON mapping to this file (default: stdout only).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)
    url = (args.url or "").strip() or default_section_assignment_url(cfg)
    folder_key = (args.folder_key or discover_folder_key(cfg)).strip().lower()

    state_path = args.storage or storage_state_path(cfg)
    if not args.skip_scrape and not state_path.is_file():
        print(
            f"Missing storage state file: {state_path}\n"
            "Run: python3 src/login_session.py\n"
            "Or use --skip-scrape with a students.json file.",
            file=sys.stderr,
        )
        return 2

    hand_path = args.students_json
    if hand_path is None and not args.no_students_json:
        hand_path = discover_students_json_path(cfg)
    hand: dict[str, str] = {}
    if hand_path is not None and not args.no_students_json:
        hand = load_students_json(hand_path)

    scraped: dict[str, str] = {}
    warnings: list[str] = []

    if not args.skip_scrape:
        if not url:
            print(
                "No section assignment URL. Set paperless.section_assignment_url in config.yaml "
                "or CS198_SECTION_ASSIGNMENT_URL, or pass --url. "
                "Alternatively use --skip-scrape with a students.json file.",
                file=sys.stderr,
            )
            return 2
        scraped, warnings = discover_mapping_from_page(
            section_url=url,
            storage_state=state_path,
            browser_name=playwright_browser(cfg),
            headless=not args.headed,
            folder_key=folder_key,
            wait_ms=args.wait_ms,
        )

    merged = merge_mappings(scraped, hand, hand_overrides=args.hand_override)

    if not merged and not hand and args.skip_scrape:
        print(
            "No data: provide a non-empty students.json or run without --skip-scrape.",
            file=sys.stderr,
        )
        return 2

    for w in warnings:
        print(w, file=sys.stderr)

    payload: dict[str, Any] = {
        "folder_key": folder_key,
        "mapping": merged,
        "sources": {
            "scraped_keys": sorted(scraped.keys()),
            "students_json_keys": sorted(hand.keys()) if hand else [],
        },
    }

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {len(merged)} entr(y/ies) to {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
