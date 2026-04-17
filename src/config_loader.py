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
