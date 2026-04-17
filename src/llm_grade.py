#!/usr/bin/env python3
"""
Call OpenAI or Anthropic to produce structured scores + comment from a scraped
rubric schema and autograder summary (optional code snippets).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    llm_code_globs,
    llm_max_code_chars,
    llm_model,
    llm_provider,
    load_config,
)


def collect_code_snippets(folder: Path, globs: list[str], max_chars: int) -> str:
    if max_chars <= 0:
        return ""
    parts: list[str] = []
    total = 0
    for pattern in globs:
        try:
            paths = sorted(folder.glob(pattern))
        except ValueError:
            continue
        for p in paths:
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(folder)
            except ValueError:
                rel = p.name
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            chunk = f"=== {rel} ===\n{text}\n"
            if total + len(chunk) > max_chars:
                chunk = chunk[: max(0, max_chars - total)]
            if not chunk:
                break
            parts.append(chunk)
            total += len(chunk)
            if total >= max_chars:
                return "".join(parts)
    return "".join(parts)


def rubric_field_keys(schema: dict[str, Any]) -> list[str]:
    """Schema keys that need a score (excludes comment role for separate comment string)."""
    out: list[str] = []
    for f in schema.get("fields") or []:
        if not isinstance(f, dict):
            continue
        if f.get("role") == "comment":
            continue
        k = f.get("key")
        if k:
            out.append(str(k))
    return out


def _parse_float(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except ValueError:
        return None


def _max_for_field(field: dict[str, Any]) -> float | None:
    mp = field.get("max_points")
    if mp is None:
        return None
    return _parse_float(mp)


def clamp_scores_to_schema(
    scores: dict[str, Any],
    schema: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Clamp numeric rubric values to max_points / min; drop unknown keys. Returns (clamped, warnings)."""
    warnings: list[str] = []
    fields_by_key: dict[str, dict[str, Any]] = {}
    for f in schema.get("fields") or []:
        if isinstance(f, dict) and f.get("key"):
            fields_by_key[str(f["key"])] = f

    out: dict[str, Any] = {}
    for k, v in scores.items():
        fk = str(k)
        if fk not in fields_by_key:
            warnings.append(f"Ignoring unknown score key {fk!r}")
            continue
        field = fields_by_key[fk]
        if field.get("role") == "comment":
            continue
        ctl = (field.get("control_type") or "").lower()
        dom = field.get("dom") or {}
        if ctl == "radio_group":
            out[fk] = v
            continue
        if ctl == "select":
            out[fk] = v
            continue
        if ctl in ("number", "range") or (
            ctl == "textarea" and field.get("role") == "rubric"
        ):
            n = _parse_float(v)
            if n is None:
                warnings.append(f"Could not parse number for {fk!r}: {v!r}")
                continue
            mx = _max_for_field(field)
            dom_max = _parse_float(dom.get("max"))
            dom_min = _parse_float(dom.get("min"))
            hi = mx if mx is not None else dom_max
            lo = dom_min if dom_min is not None else 0.0
            if hi is not None:
                n = min(n, hi)
            if lo is not None:
                n = max(n, lo)
            out[fk] = n
            continue
        # text-like
        out[fk] = v

    # Ensure every rubric key present (fill missing with None for caller to handle)
    for fk in rubric_field_keys(schema):
        if fk not in out:
            warnings.append(f"Missing score for required rubric key {fk!r}")

    return out, warnings


def normalize_llm_payload(raw: Any) -> dict[str, Any]:
    """Normalize LLM output to {"scores": {...}, "comment": str} (scores not yet clamped)."""
    if not isinstance(raw, dict):
        return {"scores": {}, "comment": ""}

    scores_in = raw.get("scores")
    if scores_in is None and isinstance(raw.get("values"), dict):
        scores_in = raw["values"]
    if not isinstance(scores_in, dict):
        scores_in = {}

    comment = raw.get("comment")
    if comment is None:
        comment = raw.get("feedback") or raw.get("comments") or ""
    comment = "" if comment is None else str(comment)

    return {"scores": dict(scores_in), "comment": comment}


_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else {}
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            v = json.loads(m.group(1).strip())
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            v = json.loads(text[start : end + 1])
            return v if isinstance(v, dict) else {}
        except json.JSONDecodeError:
            pass
    return {}


def build_grading_prompt(
    schema: dict[str, Any],
    autograder_summary: dict[str, Any],
    *,
    code_snippets: str = "",
) -> tuple[str, str]:
    """Return (system_prompt, user_prompt)."""
    system = (
        "You are a CS course grader assistant. You must assign scores only using the "
        "rubric field keys provided in the user message. Base deductions on autograder "
        "failures when the rubric relates to correctness; note style issues briefly when "
        "relevant. Be fair and consistent. "
        "Respond with a single JSON object only (no markdown), with exactly two top-level keys: "
        '"scores" (object mapping each rubric field key string to a number, string, or boolean as appropriate for that control type) '
        'and "comment" (string: concise feedback for the student comment box). '
        "Do not invent rubric keys; use every rubric key listed. "
        'If unsure, prefer conservative scores and explain in "comment".'
    )
    user_parts: list[str] = []
    user_parts.append("## Rubric schema (JSON)\n" + json.dumps(schema, indent=2, ensure_ascii=False))
    user_parts.append(
        "\n## Autograder summary (JSON)\n"
        + json.dumps(autograder_summary, indent=2, ensure_ascii=False)
    )
    if code_snippets.strip():
        user_parts.append("\n## Student code (excerpt)\n" + code_snippets)
    user = "\n".join(user_parts)
    return system, user


def grade_with_openai(
    model: str,
    system: str,
    user: str,
) -> dict[str, Any]:
    from openai import OpenAI

    client = OpenAI()
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
    text = (resp.choices[0].message.content or "").strip()
    return _extract_json_object(text)


def grade_with_anthropic(
    model: str,
    system: str,
    user: str,
) -> dict[str, Any]:
    import anthropic

    client = anthropic.Anthropic()
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts: list[str] = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    text = "".join(parts)
    return _extract_json_object(text)


def grade_submission(
    schema: dict[str, Any],
    autograder_summary: dict[str, Any],
    *,
    folder: Path | None = None,
    provider: str | None = None,
    model: str | None = None,
    code_globs: list[str] | None = None,
    max_code_chars: int | None = None,
) -> dict[str, Any]:
    """
    Return {"scores": {...}, "comment": "...", "warnings": [...], "raw_llm": ...}.
    """
    prov = (provider or "openai").strip().lower()
    mod = model or "gpt-4o-mini"
    globs = code_globs or ["*.py", "**/*.py"]
    max_c = 12000 if max_code_chars is None else max(0, int(max_code_chars))

    code = ""
    if folder is not None and folder.is_dir():
        code = collect_code_snippets(folder, globs, max_c)

    system, user = build_grading_prompt(
        schema,
        autograder_summary,
        code_snippets=code,
    )

    if prov == "anthropic":
        raw = grade_with_anthropic(mod, system, user)
    else:
        raw = grade_with_openai(mod, system, user)

    normalized = normalize_llm_payload(raw)
    scores, warnings = clamp_scores_to_schema(normalized["scores"], schema)

    merged = {
        "scores": scores,
        "comment": normalized.get("comment") or "",
        "warnings": warnings,
        "raw_llm": raw,
    }
    return merged


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="LLM: rubric schema + autograder -> scores JSON.")
    p.add_argument("--config", type=Path, default=None, help="config.yaml path.")
    p.add_argument(
        "--schema",
        type=Path,
        required=True,
        help="JSON file from scrape_grading_form (LLM rubric schema).",
    )
    p.add_argument(
        "--summary",
        type=Path,
        required=True,
        help="Per-student autograder summary JSON (e.g. *.summary.json from run_autograder).",
    )
    p.add_argument(
        "--folder",
        type=Path,
        default=None,
        help="Submission folder for optional code snippets.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write result JSON (default: stdout).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)

    schema_text = Path(args.schema).read_text(encoding="utf-8")
    schema = json.loads(schema_text)
    if not isinstance(schema, dict):
        print("Schema JSON must be an object.", file=sys.stderr)
        return 2

    summary_text = Path(args.summary).read_text(encoding="utf-8")
    summary = json.loads(summary_text)
    if not isinstance(summary, dict):
        print("Summary JSON must be an object.", file=sys.stderr)
        return 2

    folder = Path(args.folder).expanduser() if args.folder else None

    result = grade_submission(
        schema,
        summary,
        folder=folder,
        provider=llm_provider(cfg),
        model=llm_model(cfg),
        code_globs=llm_code_globs(cfg),
        max_code_chars=llm_max_code_chars(cfg),
    )
    # Drop raw from default output to keep files smaller (optional)
    out_payload = {
        "scores": result["scores"],
        "comment": result["comment"],
        "warnings": result["warnings"],
    }
    text = json.dumps(out_payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
