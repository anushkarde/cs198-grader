#!/usr/bin/env python3
"""
Load a Paperless grading page and serialize rubric controls to a JSON schema for
the LLM: stable keys, human-readable labels, control types, and max points when
discernible (from labels or HTML constraints).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    default_probe_grading_url,
    load_config,
    playwright_browser,
    storage_state_path,
)
from src.form_field_probe import collect_form_fields_json  # noqa: E402
from src.playwright_util import launch_browser, new_context_with_storage  # noqa: E402

# e.g. "(5 pts)", "3 points", "10 pt"
_POINTS_IN_LABEL_RE = re.compile(
    r"(?P<pts>\d+(?:\.\d+)?)\s*(?:/\s*\d+(?:\.\d+)?\s*)?(?:pt|pts|point|points)\b",
    re.IGNORECASE,
)

_SCHEMA_VERSION = 1

# Inputs that are not rubric / comment fields for grading.
_SKIP_INPUT_TYPES = frozenset({"submit", "button", "reset", "image", "hidden", "file"})

_COMMENT_LABEL_RE = re.compile(
    r"\b(comment|comments|feedback|overall\s+feedback|grader\s+notes?|notes)\b",
    re.IGNORECASE,
)


def _parse_float(s: Any) -> float | None:
    if s is None or s == "":
        return None
    try:
        return float(str(s).strip())
    except ValueError:
        return None


def infer_max_points_from_label(label: str | None) -> float | None:
    """Extract a point value from rubric-style label text, if present."""
    if not label:
        return None
    m = _POINTS_IN_LABEL_RE.search(label)
    if not m:
        return None
    return _parse_float(m.group("pts"))


def _infer_comment_role(label: str | None, tag: str, typ: str) -> bool:
    if tag != "textarea":
        return False
    if not label:
        return False
    return bool(_COMMENT_LABEL_RE.search(label))


def _stable_key_for_radio_group(name: str, idx: int, used: set[str]) -> str:
    base = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(name)).strip("_") or f"radio_group_{idx}"
    if base not in used:
        return base
    n = 2
    while f"{base}_{n}" in used:
        n += 1
    return f"{base}_{n}"


def _stable_key_for_field(f: dict[str, Any], idx: int, used: set[str]) -> str:
    typ = (f.get("type") or "").lower()
    tag = (f.get("tag") or "").lower()
    name = f.get("name")
    if typ in ("radio", "checkbox") and name:
        base = f"{name}:{f.get('value', '')}"
    elif name:
        base = str(name)
    elif f.get("id"):
        base = str(f["id"])
    else:
        base = f"field_{idx}_{tag}_{typ}"
    key = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", base).strip("_") or f"field_{idx}"
    if key not in used:
        return key
    n = 2
    while f"{key}_{n}" in used:
        n += 1
    return f"{key}_{n}"


def _normalize_control_type(tag: str, typ: str) -> str:
    t = typ.lower()
    if tag == "textarea":
        return "textarea"
    if tag == "select":
        return "select"
    if tag == "input":
        if t in ("number", "range", "text", "email", "url", "search", "tel"):
            return t
        return t
    return t


def _should_skip_field(f: dict[str, Any]) -> bool:
    tag = (f.get("tag") or "").lower()
    typ = (f.get("type") or "").lower()
    if tag == "input" and typ in _SKIP_INPUT_TYPES:
        return True
    return False


def _dom_snapshot(f: dict[str, Any]) -> dict[str, Any]:
    """Subset of raw probe data for fill / debugging (stable attributes)."""
    out: dict[str, Any] = {
        "tag": f.get("tag"),
        "type": f.get("type"),
        "name": f.get("name"),
        "id": f.get("id"),
    }
    for k in ("min", "max", "step", "placeholder", "required", "disabled"):
        if f.get(k) is not None:
            out[k] = f[k]
    if f.get("options"):
        out["options"] = f["options"]
    return out


def _max_points_for_scalar_field(f: dict[str, Any]) -> float | None:
    label = f.get("label")
    from_label = infer_max_points_from_label(label) if label else None
    typ = (f.get("type") or "").lower()
    tag = (f.get("tag") or "").lower()
    dom_max = _parse_float(f.get("max"))
    if from_label is not None:
        return from_label
    if typ == "number" or typ == "range" or tag == "textarea":
        return dom_max
    return None


def raw_field_to_schema_entry(
    f: dict[str, Any],
    idx: int,
    used_keys: set[str],
) -> dict[str, Any]:
    key = _stable_key_for_field(f, idx, used_keys)
    used_keys.add(key)
    tag = (f.get("tag") or "").lower()
    typ = (f.get("type") or "").lower()
    label = f.get("label")
    control_type = _normalize_control_type(tag, typ)
    role = "comment" if _infer_comment_role(label, tag, typ) else "rubric"
    entry: dict[str, Any] = {
        "key": key,
        "label": label,
        "section_hint": f.get("section_hint"),
        "control_type": control_type,
        "max_points": _max_points_for_scalar_field(f),
        "role": role,
        "dom": _dom_snapshot(f),
    }
    if typ in ("radio", "checkbox"):
        entry["value"] = f.get("value")
        entry["checked"] = f.get("checked")
    return entry


def _merge_radio_group(group: list[dict[str, Any]], idx0: int, used_keys: set[str]) -> dict[str, Any]:
    first = group[0]
    name = first.get("name") or f"radio_{idx0}"
    key = _stable_key_for_radio_group(str(name), idx0, used_keys)
    used_keys.add(key)
    labels = [g.get("label") for g in group if g.get("label")]
    # Often every radio repeats the same question label; use the longest as the group label.
    label = max(labels, key=len) if labels else str(name)
    max_from_labels: float | None = None
    for g in group:
        mp = infer_max_points_from_label(g.get("label"))
        if mp is not None:
            max_from_labels = mp if max_from_labels is None else max(max_from_labels, mp)
    options: list[dict[str, Any]] = []
    for g in group:
        opt_label = g.get("label") or str(g.get("value", ""))
        options.append(
            {
                "value": g.get("value"),
                "label": opt_label,
                "id": g.get("id"),
                "checked": g.get("checked"),
            }
        )
    section = first.get("section_hint")
    return {
        "key": key,
        "label": label,
        "section_hint": section,
        "control_type": "radio_group",
        "max_points": max_from_labels,
        "role": "rubric",
        "dom": {
            "tag": "input",
            "type": "radio",
            "name": name,
            "ids": [g.get("id") for g in group],
        },
        "options": options,
    }


def merge_radio_groups(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse same-name radio inputs into one schema field with options."""
    by_name: dict[str, list[dict[str, Any]]] = {}
    for f in raw:
        if (f.get("type") or "").lower() == "radio" and f.get("name"):
            by_name.setdefault(str(f["name"]), []).append(f)
    out: list[dict[str, Any]] = []
    used: set[str] = set()
    idx_counter = 0
    for f in raw:
        if _should_skip_field(f):
            continue
        typ = (f.get("type") or "").lower()
        if typ == "radio" and f.get("name"):
            name = str(f["name"])
            group = by_name.get(name) or [f]
            if group and group[0] is f:
                out.append(_merge_radio_group(group, idx_counter, used))
                idx_counter += 1
            continue
        out.append(raw_field_to_schema_entry(f, idx_counter, used))
        idx_counter += 1
    return out


def build_llm_schema(
    raw_fields: list[dict[str, Any]],
    *,
    source_url: str = "",
) -> dict[str, Any]:
    """
    Build the JSON object passed to the LLM: rubric fields with keys, labels,
    types, and max_points; plus which field holds free-form comments (if any).
    """
    merged = merge_radio_groups(raw_fields)
    rubric_fields: list[dict[str, Any]] = []
    comment_key: str | None = None
    comment_label: str | None = None
    for entry in merged:
        if entry.get("role") == "comment" and entry.get("key"):
            comment_key = str(entry["key"])
            comment_label = entry.get("label")
        rubric_fields.append(entry)

    return {
        "schema_version": _SCHEMA_VERSION,
        "source_url": source_url,
        "fields": rubric_fields,
        "comment_field": (
            {"key": comment_key, "label": comment_label}
            if comment_key
            else None
        ),
    }


def scrape_grading_schema(page, *, source_url: str = "") -> dict[str, Any]:
    raw = collect_form_fields_json(page)
    return build_llm_schema(raw, source_url=source_url)


def schema_to_json_text(schema: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(schema, indent=indent, ensure_ascii=False) + "\n"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Scrape a Paperless grading page into an LLM rubric JSON schema.",
    )
    p.add_argument(
        "--url",
        default="",
        help="Grading page URL (default: paperless.probe_grading_url or CS198_PROBE_GRADING_URL).",
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
    return p.parse_args()


def main() -> int:
    from playwright.sync_api import sync_playwright

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
            schema = scrape_grading_schema(page, source_url=url)
        finally:
            browser.close()

    sys.stdout.write(schema_to_json_text(schema))
    n = len(schema.get("fields") or [])
    print(f"Serialized {n} field(s).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
