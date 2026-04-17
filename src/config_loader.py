"""Load config.yaml with env overrides."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

from src.paths import config_path as default_config_path
from src.paths import repo_root


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or default_config_path()
    if not p.is_file():
        return {}
    with open(p, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        return {}
    return data


def storage_state_path(cfg: dict[str, Any]) -> Path:
    raw = os.environ.get("CS198_STORAGE_STATE")
    if raw:
        return Path(raw).expanduser()
    rel = (
        (cfg.get("playwright") or {}).get("storage_state_path")
        or "storage_state.json"
    )
    return repo_root() / str(rel)


def default_probe_grading_url(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_PROBE_GRADING_URL", "").strip()
    if env:
        return env
    return str((cfg.get("paperless") or {}).get("probe_grading_url") or "").strip()


def default_section_assignment_url(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_SECTION_ASSIGNMENT_URL", "").strip()
    if env:
        return env
    return str((cfg.get("paperless") or {}).get("section_assignment_url") or "").strip()


def discover_folder_key(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_DISCOVER_FOLDER_KEY", "").strip().lower()
    if env in ("sunet", "submission_id"):
        return env
    raw = (cfg.get("discover") or {}).get("folder_key") or "submission_id"
    s = str(raw).strip().lower()
    return s if s in ("sunet", "submission_id") else "submission_id"


def discover_students_json_path(cfg: dict[str, Any]) -> Path | None:
    env = os.environ.get("CS198_STUDENTS_JSON", "").strip()
    if env:
        return Path(env).expanduser()
    rel = (cfg.get("discover") or {}).get("students_json_path")
    if rel is None or str(rel).strip() == "":
        return None
    return repo_root() / str(rel).strip()


def playwright_browser(cfg: dict[str, Any]) -> str:
    name = (cfg.get("playwright") or {}).get("browser") or "chromium"
    return str(name).lower()


def autograder_submissions_root(cfg: dict[str, Any]) -> Path | None:
    env = os.environ.get("CS198_SUBMISSIONS_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    raw = (cfg.get("autograder") or {}).get("submissions_root")
    if raw is None or str(raw).strip() == "":
        return None
    p = Path(str(raw).strip())
    return p if p.is_absolute() else repo_root() / p


def autograder_logs_dir(cfg: dict[str, Any]) -> Path:
    env = os.environ.get("CS198_AUTOGRADER_LOGS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    rel = (cfg.get("autograder") or {}).get("logs_dir") or ".grader_cache/autograder_logs"
    p = Path(str(rel).strip())
    return p if p.is_absolute() else repo_root() / p


def autograder_python(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_PYTHON", "").strip()
    if env:
        return env
    return str((cfg.get("autograder") or {}).get("python") or "python3")


def autograder_script_name(cfg: dict[str, Any]) -> str:
    return str((cfg.get("autograder") or {}).get("script_name") or "KarelAutograder.py")


def llm_provider(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_LLM_PROVIDER", "").strip().lower()
    if env in ("openai", "anthropic"):
        return env
    raw = (cfg.get("llm") or {}).get("provider") or "openai"
    s = str(raw).strip().lower()
    return s if s in ("openai", "anthropic") else "openai"


def llm_model(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_LLM_MODEL", "").strip()
    if env:
        return env
    return str((cfg.get("llm") or {}).get("model") or "gpt-4o-mini")


def llm_max_code_chars(cfg: dict[str, Any]) -> int:
    env = os.environ.get("CS198_LLM_MAX_CODE_CHARS", "").strip()
    if env:
        try:
            return max(0, int(env))
        except ValueError:
            return 12000
    raw = (cfg.get("llm") or {}).get("max_code_chars")
    try:
        return max(0, int(raw)) if raw is not None else 12000
    except (TypeError, ValueError):
        return 12000


def llm_code_globs(cfg: dict[str, Any]) -> list[str]:
    raw = (cfg.get("llm") or {}).get("code_globs")
    if isinstance(raw, list) and raw:
        return [str(x) for x in raw]
    return ["*.py", "**/*.py"]


def fill_save_button_selector(cfg: dict[str, Any]) -> str:
    env = os.environ.get("CS198_SAVE_BUTTON_SELECTOR", "").strip()
    if env:
        return env
    return str(
        (cfg.get("fill") or {}).get("save_button_selector")
        or 'button[type="submit"], input[type="submit"], button:has-text("Save")'
    )


def autograder_timeout_sec(cfg: dict[str, Any]) -> float | None:
    env = os.environ.get("CS198_AUTOGRADER_TIMEOUT_SEC", "").strip()
    if env:
        try:
            v = float(env)
            return None if v <= 0 else v
        except ValueError:
            return None
    raw = (cfg.get("autograder") or {}).get("timeout_sec")
    if raw is None:
        return 600.0
    try:
        v = float(raw)
        return None if v <= 0 else v
    except (TypeError, ValueError):
        return 600.0
