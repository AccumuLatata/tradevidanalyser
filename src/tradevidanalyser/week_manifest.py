"""Weekly manifest gate for ``TVA_NET_FEES`` publish (TT:903)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Literal

WEEK_MANIFEST_NOT_LOCKED = "Wochenmanifest nicht locked"
WEEK_MANIFEST_MISSING = "Wochenmanifest fehlt"
WEEK_MANIFEST_UNREADABLE = "Wochenmanifest unlesbar"
STATUS_LOCKED = "locked"
STATUS_OPEN = "open"
WeekStatus = Literal["locked", "open"]


class WeekManifestError(ValueError):
    """Weekly manifest is missing, open, or unreadable."""


def iso_week_id(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weeks_dir(root: Path) -> Path:
    return Path(root) / "weeks"


def week_dir(root: Path, week: str) -> Path:
    return weeks_dir(root) / week


def week_manifest_path(root: Path, week: str) -> Path:
    return week_dir(root, week) / "manifest.json"


def write_week_manifest(
    root: Path,
    day: date,
    *,
    status: WeekStatus,
) -> Path:
    week = iso_week_id(day)
    path = week_manifest_path(root, week)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"week": week, "status": status}
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def load_week_manifest(root: Path, day: date) -> dict[str, Any]:
    week = iso_week_id(day)
    path = week_manifest_path(root, week)
    if not path.is_file():
        raise WeekManifestError(f"{WEEK_MANIFEST_MISSING}: {week}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeekManifestError(f"{WEEK_MANIFEST_UNREADABLE}: {week}") from exc
    if not isinstance(payload, dict):
        raise WeekManifestError(f"{WEEK_MANIFEST_UNREADABLE}: {week}")
    return payload


def require_locked_week_manifest(root: Path, day: date) -> dict[str, Any]:
    week = iso_week_id(day)
    payload = load_week_manifest(root, day)
    status = payload.get("status")
    if status != STATUS_LOCKED:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}")
    return payload
