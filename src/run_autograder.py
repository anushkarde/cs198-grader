#!/usr/bin/env python3
"""
Run `python3 KarelAutograder.py --all` in each submission folder under a root
directory. Writes full logs and a compact JSON summary for downstream LLM use.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src.config_loader import (  # noqa: E402
    autograder_logs_dir,
    autograder_python,
    autograder_script_name,
    autograder_submissions_root,
    autograder_timeout_sec,
    load_config,
)
from src.paths import repo_root  # noqa: E402

_FAILURE_TAIL_LINES = 40

# Best-effort lines like "SomeKarel ... PASS" or "PASS: SomeKarel"
_LINE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)^\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\.py)?\s*"
        r"[:.)-]+\s*(?P<st>PASS|FAIL|OK|passed|failed)\b"
    ),
    re.compile(
        r"(?i)(?P<st>PASS|FAIL|OK|passed|failed)\s*[:.)-]+\s*"
        r"(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\.py)?\b"
    ),
)


def _status_to_passed(st: str) -> bool:
    s = st.lower()
    if s in ("pass", "ok", "passed"):
        return True
    if s in ("fail", "failed"):
        return False
    return False


def parse_autograder_text(text: str) -> dict[str, Any]:
    """
    Heuristic parse of KarelAutograder (or similar) stdout/stderr.
    Returns programs (last mention per name), pass/fail counts, optional notes.
    """
    programs: dict[str, bool] = {}
    for line in text.splitlines():
        for pat in _LINE_PATTERNS:
            m = pat.search(line.strip())
            if not m:
                continue
            name = m.group("name")
            st = m.group("st")
            programs[name] = _status_to_passed(st)
            break

    plist = [{"name": n, "passed": p} for n, p in sorted(programs.items())]
    passed_n = sum(1 for x in plist if x["passed"])
    failed_n = len(plist) - passed_n
    return {
        "programs": plist,
        "program_count": len(plist),
        "passed_count": passed_n,
        "failed_count": failed_n,
    }


def _failure_tail(text: str, *, max_lines: int = _FAILURE_TAIL_LINES) -> str | None:
    if not text.strip():
        return None
    lines = text.splitlines()
    tail = lines[-max_lines:] if len(lines) > max_lines else lines
    return "\n".join(tail)


def _write_log(path: Path, folder: str, combined: str, meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        f"=== run_autograder.py ===\n"
        f"folder: {folder}\n"
        f"exit_code: {meta.get('exit_code')}\n"
        f"timed_out: {meta.get('timed_out')}\n"
        f"=== KarelAutograder.py --all (stdout+stderr) ===\n"
    )
    path.write_text(header + (combined or ""), encoding="utf-8")


def run_autograder_in_folder(
    folder: Path,
    *,
    python_exe: str,
    script_name: str,
    timeout_sec: float | None,
) -> dict[str, Any]:
    """Run autograder in `folder`; return one summary record (includes error fields)."""
    script_path = folder / script_name
    try:
        rel_folder = str(folder.resolve().relative_to(repo_root().resolve()))
    except ValueError:
        rel_folder = str(folder)

    base: dict[str, Any] = {
        "folder": folder.name,
        "folder_path": str(folder.resolve()),
        "folder_path_repo_relative": rel_folder,
        "script_path": str(script_path),
        "script_exists": script_path.is_file(),
    }

    if not script_path.is_file():
        base.update(
            {
                "ok": False,
                "exit_code": None,
                "timed_out": False,
                "error": f"missing {script_name}",
                "stdout": "",
                "stderr": "",
                "combined": "",
                "parsed": parse_autograder_text(""),
                "failure_tail": None,
                "duration_ms": 0,
            }
        )
        return base

    cmd = [python_exe, script_name, "--all"]
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=folder,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            check=False,
        )
        duration_ms = int((time.perf_counter() - t0) * 1000)
        out = proc.stdout or ""
        err = proc.stderr or ""
        combined = ""
        if out:
            combined += "--- stdout ---\n" + out
        if err:
            if combined:
                combined += "\n"
            combined += "--- stderr ---\n" + err
        parsed = parse_autograder_text(combined)
        plist = parsed.get("programs") or []
        has_fail_parsed = any(not p.get("passed") for p in plist if isinstance(p, dict))
        exit_code = proc.returncode
        ok = exit_code == 0 and not has_fail_parsed
        failure_tail = None
        if not ok:
            failure_tail = _failure_tail(combined) or _failure_tail(out) or _failure_tail(err)

        base.update(
            {
                "ok": ok,
                "exit_code": exit_code,
                "timed_out": False,
                "error": None,
                "stdout": out,
                "stderr": err,
                "combined": combined,
                "parsed": parsed,
                "failure_tail": failure_tail,
                "duration_ms": duration_ms,
            }
        )
        return base
    except subprocess.TimeoutExpired as e:
        duration_ms = int((time.perf_counter() - t0) * 1000)
        out = (e.stdout or "") if isinstance(e.stdout, str) else ""
        err = (e.stderr or "") if isinstance(e.stderr, str) else str(e)
        combined = ""
        if out:
            combined += "--- stdout ---\n" + out
        if err:
            if combined:
                combined += "\n"
            combined += "--- stderr ---\n" + err
        base.update(
            {
                "ok": False,
                "exit_code": None,
                "timed_out": True,
                "error": "timeout",
                "stdout": out,
                "stderr": err,
                "combined": combined,
                "parsed": parse_autograder_text(combined),
                "failure_tail": _failure_tail(combined) or str(e),
                "duration_ms": duration_ms,
            }
        )
        return base


def iter_submission_folders(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in sorted(root.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            out.append(p)
    return out


def run_all(
    submissions_root: Path,
    logs_dir: Path,
    *,
    python_exe: str,
    script_name: str,
    timeout_sec: float | None,
    only: frozenset[str] | None,
) -> dict[str, Any]:
    """Run autograder for each folder; write per-folder .log and summary.json; return payload."""
    folders = iter_submission_folders(submissions_root)
    if only is not None:
        folders = [f for f in folders if f.name in only]

    logs_dir = logs_dir.resolve()
    summaries: list[dict[str, Any]] = []
    t0 = time.perf_counter()

    for folder in folders:
        rec = run_autograder_in_folder(
            folder,
            python_exe=python_exe,
            script_name=script_name,
            timeout_sec=timeout_sec,
        )
        log_path = logs_dir / f"{folder.name}.log"
        meta = {
            "exit_code": rec.get("exit_code"),
            "timed_out": rec.get("timed_out"),
        }
        _write_log(log_path, folder.name, str(rec.get("combined") or ""), meta)
        rec["log_path"] = str(log_path)

        compact = {
            "folder": rec["folder"],
            "folder_path": rec["folder_path"],
            "ok": rec["ok"],
            "exit_code": rec["exit_code"],
            "timed_out": rec["timed_out"],
            "script_exists": rec["script_exists"],
            "duration_ms": rec["duration_ms"],
            "log_path": rec["log_path"],
            "programs": (rec.get("parsed") or {}).get("programs"),
            "passed_count": (rec.get("parsed") or {}).get("passed_count"),
            "failed_count": (rec.get("parsed") or {}).get("failed_count"),
            "failure_tail": rec.get("failure_tail"),
            "error": rec.get("error"),
        }
        summaries.append(compact)

        summary_path = logs_dir / f"{folder.name}.summary.json"
        summary_path.write_text(
            json.dumps(compact, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "submissions_root": str(submissions_root.resolve()),
        "logs_dir": str(logs_dir),
        "python": python_exe,
        "script_name": script_name,
        "timeout_sec": timeout_sec,
        "total_elapsed_ms": elapsed_ms,
        "folder_count": len(summaries),
        "results": summaries,
    }


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run KarelAutograder.py --all in each submission folder; save logs and JSON summaries.",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to config.yaml (default: ./config.yaml).",
    )
    p.add_argument(
        "--submissions-root",
        type=Path,
        default=None,
        help="Root directory of per-student folders (default: autograder.submissions_root or CS198_SUBMISSIONS_ROOT).",
    )
    p.add_argument(
        "--logs-dir",
        type=Path,
        default=None,
        help="Where to write *.log and *.summary.json (default: autograder.logs_dir / CS198_AUTOGRADER_LOGS_DIR).",
    )
    p.add_argument(
        "--only",
        nargs="*",
        default=[],
        help="Only run these folder names (basename), not all siblings.",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write combined JSON report to this file (default: stdout only).",
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    cfg = load_config(args.config)

    root = args.submissions_root or autograder_submissions_root(cfg)
    if root is None:
        print(
            "Set autograder.submissions_root in config.yaml or CS198_SUBMISSIONS_ROOT, "
            "or pass --submissions-root.",
            file=sys.stderr,
        )
        return 2
    root = root.expanduser().resolve()
    if not root.is_dir():
        print(f"Not a directory: {root}", file=sys.stderr)
        return 2

    logs_dir = (args.logs_dir or autograder_logs_dir(cfg)).expanduser().resolve()
    only_set: frozenset[str] | None = frozenset(args.only) if args.only else None

    payload = run_all(
        root,
        logs_dir,
        python_exe=autograder_python(cfg),
        script_name=autograder_script_name(cfg),
        timeout_sec=autograder_timeout_sec(cfg),
        only=only_set,
    )

    text = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(
            f"Wrote {payload['folder_count']} result(s); report: {args.output}",
            file=sys.stderr,
        )
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
