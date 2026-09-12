"""Shared store root. Same path convention on the trading PC, Mac, and laptop."""

from __future__ import annotations

import os
from pathlib import Path

ENV_ROOT = "TVA_ROOT"
DEFAULT_STORE_NAME = ".tva_store"
RECORDINGS_DIR = "recordings"
SESSIONS_DIR = "sessions"


def resolve_root(cli_root: Path | str | None = None) -> Path:
    """CLI --root, then TVA_ROOT, then <cwd>/.tva_store."""
    if cli_root:
        return Path(cli_root).expanduser().resolve()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser().resolve()
    return (Path.cwd() / DEFAULT_STORE_NAME).resolve()


def recordings_dir(root: Path) -> Path:
    return root / RECORDINGS_DIR


def sessions_dir(root: Path) -> Path:
    return root / SESSIONS_DIR


def session_dir(root: Path, session_id: str) -> Path:
    return sessions_dir(root) / session_id


def ensure_layout(root: Path) -> None:
    recordings_dir(root).mkdir(parents=True, exist_ok=True)
    sessions_dir(root).mkdir(parents=True, exist_ok=True)
