"""Weekly lock gate for ``TVA_NET_FEES`` (TT JX8).

Reads ThesisTester ``week_manifest.json`` (``lock_status``, ``week``,
``account``, ``schema_version: journal/v1``). A hand-written file cannot
unlock publish: verification must go through TT ``lock.py`` (2195-2219).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

WEEK_MANIFEST_NAME = "week_manifest.json"
WEEK_MANIFEST_NOT_LOCKED = "Wochenmanifest nicht locked"
WEEK_MANIFEST_MISSING = "Wochenmanifest fehlt"
WEEK_MANIFEST_UNREADABLE = "Wochenmanifest unlesbar"
WEEK_MANIFEST_MISMATCH = "Wochenmanifest week/account stimmen nicht"
SCHEMA_VERSION = "journal/v1"
STATUS_LOCKED = "locked"


class WeekManifestError(ValueError):
    """Weekly manifest is missing, open, mismatched, or unverified."""


def iso_week_id(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def week_manifest_path(root: Path) -> Path:
    direct = Path(root) / WEEK_MANIFEST_NAME
    if direct.is_file():
        return direct
    nested = Path(root) / "journal" / WEEK_MANIFEST_NAME
    if nested.is_file():
        return nested
    return direct


def write_week_manifest(
    root: Path,
    day: date,
    *,
    lock_status: str,
    account: str,
) -> Path:
    """Test helper. Writes TT schema; does **not** unlock publish without TT verify."""
    week = iso_week_id(day)
    path = Path(root) / WEEK_MANIFEST_NAME
    payload = {
        "schema_version": SCHEMA_VERSION,
        "week": week,
        "account": account,
        "lock_status": lock_status,
    }
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def load_week_manifest(root: Path) -> dict[str, Any]:
    path = week_manifest_path(root)
    if not path.is_file():
        raise WeekManifestError(WEEK_MANIFEST_MISSING)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WeekManifestError(WEEK_MANIFEST_UNREADABLE) from exc
    if not isinstance(payload, dict):
        raise WeekManifestError(WEEK_MANIFEST_UNREADABLE)
    return payload


def require_locked_week_manifest(
    root: Path,
    day: date,
    *,
    account: str | None,
) -> dict[str, Any]:
    """Publish only when TT re-derives ``lock_status == locked`` for this week+account."""
    from tradevidanalyser.tt_journal import (
        TT_LOCK_API_MISSING,
        call_tt_week_verify,
        load_tt_week_verify,
    )

    week = iso_week_id(day)
    payload = load_week_manifest(root)
    if payload.get("schema_version") not in {None, SCHEMA_VERSION}:
        raise WeekManifestError(WEEK_MANIFEST_UNREADABLE)
    if payload.get("week") != week:
        raise WeekManifestError(WEEK_MANIFEST_MISMATCH)
    if account is None or payload.get("account") != account:
        raise WeekManifestError(WEEK_MANIFEST_MISMATCH)
    try:
        verify = load_tt_week_verify()
    except ImportError as exc:
        raise WeekManifestError(TT_LOCK_API_MISSING) from exc
    path = week_manifest_path(root)
    try:
        verified = call_tt_week_verify(verify, path, week=week, account=account)
    except TypeError as exc:
        raise WeekManifestError(
            f"{TT_LOCK_API_MISSING}: call signature {verify!r}"
        ) from exc
    except Exception as exc:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}") from exc
    status = None
    if isinstance(verified, dict):
        status = verified.get("lock_status")
    elif isinstance(verified, str):
        status = verified
    else:
        status = getattr(verified, "lock_status", None)
    if status != STATUS_LOCKED:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}")
    return payload if isinstance(verified, dict) else {"lock_status": status, **payload}
