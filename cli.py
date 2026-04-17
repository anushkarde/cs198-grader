#!/usr/bin/env python3
"""
CS198 Paperless + Karel grader CLI: discover, autograde, schema scrape, LLM grades, fill forms.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    autograder_logs_dir,
    autograder_python,
    autograder_script_name,
    autograder_submissions_root,
    autograder_timeout_sec,
    default_section_assignment_url,
    discover_folder_key,
    discover_students_json_path,
    fill_save_button_selector,
    llm_code_globs,
    llm_max_code_chars,
    llm_model,
    llm_provider,
    load_config,
    playwright_browser,
    storage_state_path,
)
from src.discover_submissions import (  # noqa: E402
    discover_mapping_from_page,
    load_mapping_json,
    load_students_json,
    merge_mappings,
)
from src.fill_grading_page import fill_grading_session  # noqa: E402
from src.llm_grade import grade_submission  # noqa: E402
from src.paths import repo_root  # noqa: E402
from src.run_autograder import _write_log, run_autograder_in_folder  # noqa: E402
from src.scrape_grading_form import scrape_grading_schema  # noqa: E402


def _run_script(rel: str, argv: list[str]) -> int:
    script = repo_root() / rel
    p = subprocess.run([sys.executable, str(script), *argv], cwd=repo_root())
    return int(p.returncode)


def _cmd_discover(ns: argparse.Namespace) -> int:
    argv: list[str] = []
    if ns.config:
        argv += ["--config", str(ns.config)]
    if ns.url:
        argv += ["--url", ns.url]
    if ns.storage:
        argv += ["--storage", str(ns.storage)]
    if ns.headed:
        argv.append("--headed")
    if ns.wait_ms is not None:
        argv += ["--wait-ms", str(ns.wait_ms)]
    if ns.folder_key:
        argv += ["--folder-key", ns.folder_key]
    if ns.students_json:
        argv += ["--students-json", str(ns.students_json)]
    if ns.no_students_json:
        argv.append("--no-students-json")
    if not ns.hand_override:
        argv.append("--no-hand-override")
    if ns.skip_scrape:
        argv.append("--skip-scrape")
    if ns.output:
        argv += ["--output", str(ns.output)]
    return _run_script("src/discover_submissions.py", argv)


def _cmd_autograde(ns: argparse.Namespace) -> int:
    argv: list[str] = []
    if ns.config:
        argv += ["--config", str(ns.config)]
    if ns.submissions_root:
        argv += ["--submissions-root", str(ns.submissions_root)]
    if ns.logs_dir:
        argv += ["--logs-dir", str(ns.logs_dir)]
    if ns.only:
        argv += ["--only", *ns.only]
    if ns.output:
        argv += ["--output", str(ns.output)]
    return _run_script("src/run_autograder.py", argv)


def _cmd_schema(ns: argparse.Namespace) -> int:
    argv: list[str] = []
    if ns.config:
        argv += ["--config", str(ns.config)]
    if ns.url:
        argv += ["--url", ns.url]
    if ns.storage:
        argv += ["--storage", str(ns.storage)]
    if ns.headed:
        argv.append("--headed")
    if ns.wait_ms is not None:
        argv += ["--wait-ms", str(ns.wait_ms)]
    return _run_script("src/scrape_grading_form.py", argv)


def _cmd_llm(ns: argparse.Namespace) -> int:
    argv: list[str] = []
    if ns.config:
        argv += ["--config", str(ns.config)]
    argv += ["--schema", str(ns.schema), "--summary", str(ns.summary)]
    if ns.folder:
        argv += ["--folder", str(ns.folder)]
    if ns.output:
        argv += ["--output", str(ns.output)]
    return _run_script("src/llm_grade.py", argv)


def _cmd_fill(ns: argparse.Namespace) -> int:
    argv: list[str] = []
    if ns.config:
        argv += ["--config", str(ns.config)]
    argv += ["--url", ns.url, "--schema", str(ns.schema), "--grades", str(ns.grades)]
    if ns.storage:
        argv += ["--storage", str(ns.storage)]
    if ns.headed:
        argv.append("--headed")
    if ns.wait_ms is not None:
        argv += ["--wait-ms", str(ns.wait_ms)]
    if ns.dry_run:
        argv.append("--dry-run")
    if ns.review:
        argv.append("--review")
    if ns.save:
        argv.append("--save")
    return _run_script("src/fill_grading_page.py", argv)


def _build_mapping(ns: argparse.Namespace, cfg: dict) -> dict[str, str]:
    if ns.mapping and Path(ns.mapping).is_file():
        return load_mapping_json(Path(ns.mapping))

    hand_path = discover_students_json_path(cfg)
    hand: dict[str, str] = load_students_json(hand_path) if hand_path and hand_path.is_file() else {}

    if ns.skip_scrape:
        return hand

    url = (ns.section_url or "").strip() or default_section_assignment_url(cfg)
    state_path = Path(ns.storage) if ns.storage else storage_state_path(cfg)
    if not url:
        raise SystemExit(
            "No section assignment URL: set paperless.section_assignment_url, CS198_SECTION_ASSIGNMENT_URL, "
            "or pass --section-url / use --mapping file."
        )
    if not state_path.is_file():
        raise SystemExit(f"Missing storage state {state_path}; run python3 src/login_session.py or use --mapping.")

    folder_key = (ns.folder_key or discover_folder_key(cfg)).strip().lower()
    scraped, warns = discover_mapping_from_page(
        section_url=url,
        storage_state=state_path,
        browser_name=playwright_browser(cfg),
        headless=not ns.headed,
        folder_key=folder_key,
        wait_ms=ns.wait_ms or 2000,
    )
    for w in warns:
        print(w, file=sys.stderr)
    return merge_mappings(scraped, hand, hand_overrides=ns.hand_override)


def _cmd_run(ns: argparse.Namespace) -> int:
    cfg = load_config(ns.config)
    root = Path(ns.submissions_root or autograder_submissions_root(cfg) or "").expanduser()
    if not root.is_dir():
        print("Set autograder.submissions_root or pass --submissions-root.", file=sys.stderr)
        return 2

    mapping = _build_mapping(ns, cfg)
    if not mapping:
        print("Empty mapping: provide --mapping or configure discover + students.json.", file=sys.stderr)
        return 2

    logs_dir = Path(ns.logs_dir or autograder_logs_dir(cfg)).expanduser().resolve()
    llm_dir = Path(ns.llm_cache_dir or (repo_root() / ".grader_cache" / "llm")).expanduser().resolve()
    llm_dir.mkdir(parents=True, exist_ok=True)

    folders = sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("."))
    if ns.only:
        allow = frozenset(ns.only)
        folders = [f for f in folders if f.name in allow]

    state_path = Path(ns.storage) if ns.storage else storage_state_path(cfg)
    if not state_path.is_file():
        print(f"Missing storage state: {state_path}", file=sys.stderr)
        return 2

    browser_name = playwright_browser(cfg)
    from playwright.sync_api import sync_playwright

    from src.playwright_util import launch_browser, new_context_with_storage  # noqa: E402

    for folder in folders:
        key = folder.name
        url = mapping.get(key)
        if not url:
            print(f"Skip {key}: no URL in mapping.", file=sys.stderr)
            continue

        summary_path = logs_dir / f"{key}.summary.json"
        if not ns.skip_autograde:
            rec = run_autograder_in_folder(
                folder,
                python_exe=autograder_python(cfg),
                script_name=autograder_script_name(cfg),
                timeout_sec=autograder_timeout_sec(cfg),
            )
            logs_dir.mkdir(parents=True, exist_ok=True)
            log_path = logs_dir / f"{key}.log"
            meta = {"exit_code": rec.get("exit_code"), "timed_out": rec.get("timed_out")}
            _write_log(log_path, key, str(rec.get("combined") or ""), meta)
            compact = {
                "folder": rec["folder"],
                "folder_path": rec["folder_path"],
                "ok": rec["ok"],
                "exit_code": rec["exit_code"],
                "timed_out": rec["timed_out"],
                "script_exists": rec["script_exists"],
                "duration_ms": rec["duration_ms"],
                "log_path": str(log_path),
                "programs": (rec.get("parsed") or {}).get("programs"),
                "passed_count": (rec.get("parsed") or {}).get("passed_count"),
                "failed_count": (rec.get("parsed") or {}).get("failed_count"),
                "failure_tail": rec.get("failure_tail"),
                "error": rec.get("error"),
            }
            summary_path.write_text(json.dumps(compact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        elif not summary_path.is_file():
            print(f"Skip {key}: no {summary_path} (run without --skip-autograde first).", file=sys.stderr)
            continue

        summary = json.loads(summary_path.read_text(encoding="utf-8"))

        print(f"--- {key}: scrape + LLM ---", file=sys.stderr)
        with sync_playwright() as p:
            browser = launch_browser(p, browser_name, headless=not ns.headed)
            try:
                context = new_context_with_storage(browser, state_path)
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                if ns.wait_ms > 0:
                    time.sleep(ns.wait_ms / 1000.0)
                schema = scrape_grading_schema(page, source_url=url)
            finally:
                browser.close()

        result = grade_submission(
            schema,
            summary,
            folder=folder,
            provider=llm_provider(cfg),
            model=llm_model(cfg),
            code_globs=llm_code_globs(cfg),
            max_code_chars=llm_max_code_chars(cfg),
        )
        grades_out = {
            "scores": result["scores"],
            "comment": result["comment"],
            "warnings": result["warnings"],
        }
        out_json = llm_dir / f"{key}.grades.json"
        out_json.write_text(json.dumps(grades_out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        schema_path = llm_dir / f"{key}.schema.json"
        schema_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"Wrote {out_json}", file=sys.stderr)

        if ns.fill:
            actions = fill_grading_session(
                url,
                schema,
                grades_out,
                storage_state=state_path,
                browser_name=browser_name,
                headless=not ns.headed,
                dry_run=ns.dry_run,
                review=ns.review,
                save=ns.save,
                save_selector=fill_save_button_selector(cfg),
                wait_ms=ns.wait_ms,
            )
            for line in actions:
                print(line, file=sys.stderr)

    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CS198 Paperless + Karel grader assistant.")
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("discover", help="Map folder keys to submission URLs (wraps src/discover_submissions.py).")
    d.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    d.add_argument("--url", default="")
    d.add_argument("--storage", type=Path, default=None)
    d.add_argument("--headed", action="store_true")
    d.add_argument("--wait-ms", type=int, default=None)
    d.add_argument("--folder-key", choices=("sunet", "submission_id"), default=None)
    d.add_argument("--students-json", type=Path, default=None)
    d.add_argument("--no-students-json", action="store_true")
    d.add_argument("--no-hand-override", dest="hand_override", action="store_false", default=True)
    d.add_argument("--skip-scrape", action="store_true")
    d.add_argument("--output", type=Path, default=None)
    d.set_defaults(func=_cmd_discover)

    a = sub.add_parser("autograde", help="Run KarelAutograder.py --all per folder.")
    a.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    a.add_argument("--submissions-root", type=Path, default=None)
    a.add_argument("--logs-dir", type=Path, default=None)
    a.add_argument("--only", nargs="*", default=[])
    a.add_argument("--output", type=Path, default=None)
    a.set_defaults(func=_cmd_autograde)

    s = sub.add_parser("schema", help="Scrape one grading page into rubric JSON.")
    s.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    s.add_argument("--url", default="")
    s.add_argument("--storage", type=Path, default=None)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--wait-ms", type=int, default=None)
    s.set_defaults(func=_cmd_schema)

    l = sub.add_parser("llm", help="Run LLM grading from schema + autograder summary JSON.")
    l.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    l.add_argument("--schema", type=Path, required=True)
    l.add_argument("--summary", type=Path, required=True)
    l.add_argument("--folder", type=Path, default=None)
    l.add_argument("--output", type=Path, default=None)
    l.set_defaults(func=_cmd_llm)

    f = sub.add_parser("fill", help="Fill a grading page from grades JSON.")
    f.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    f.add_argument("--url", required=True)
    f.add_argument("--schema", type=Path, required=True)
    f.add_argument("--grades", type=Path, required=True)
    f.add_argument("--storage", type=Path, default=None)
    f.add_argument("--headed", action="store_true")
    f.add_argument("--wait-ms", type=int, default=None)
    f.add_argument("--dry-run", action="store_true")
    f.add_argument("--review", action="store_true")
    f.add_argument("--save", action="store_true")
    f.set_defaults(func=_cmd_fill)

    r = sub.add_parser(
        "run",
        help="End-to-end: optional autograde, scrape schema, LLM grades, optional fill per student.",
    )
    r.add_argument("--config", type=Path, default=None, help="Path to config.yaml.")
    r.add_argument("--submissions-root", type=Path, default=None)
    r.add_argument("--mapping", type=Path, default=None, help="JSON from discover (mapping key) or plain map.")
    r.add_argument("--section-url", default="", help="Override section assignment list URL.")
    r.add_argument("--storage", type=Path, default=None)
    r.add_argument("--skip-scrape", action="store_true", help="Use students.json only (see discover config).")
    r.add_argument("--folder-key", choices=("sunet", "submission_id"), default=None)
    r.add_argument("--no-hand-override", dest="hand_override", action="store_false", default=True)
    r.add_argument("--only", nargs="*", default=[], help="Only these folder basenames.")
    r.add_argument("--logs-dir", type=Path, default=None)
    r.add_argument("--llm-cache-dir", type=Path, default=None)
    r.add_argument("--skip-autograde", action="store_true", help="Use existing *.summary.json in logs dir.")
    r.add_argument("--headed", action="store_true")
    r.add_argument("--wait-ms", type=int, default=2000)
    r.add_argument("--fill", action="store_true", help="After LLM, fill the form in the browser.")
    r.add_argument("--dry-run", action="store_true", help="With --fill, log actions only (no input changes).")
    r.add_argument("--review", action="store_true", help="With --fill, pause for Enter before closing.")
    r.add_argument("--save", action="store_true", help="With --fill, click save after filling.")
    r.set_defaults(func=_cmd_run)

    return p


def main() -> int:
    parser = _build_parser()
    ns, rest = parser.parse_known_args()
    if rest:
        print(f"Unknown arguments: {' '.join(rest)}", file=sys.stderr)
        return 2

    return int(ns.func(ns))


if __name__ == "__main__":
    raise SystemExit(main())
