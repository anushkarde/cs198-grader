"""Resolve repo root and config paths."""

from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def config_path() -> Path:
    return repo_root() / "config.yaml"
