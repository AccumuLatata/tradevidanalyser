"""Weekly lock gate for ``TVA_NET_FEES`` (TT JX8).

Reads ThesisTester ``week_manifest.json`` (``lock_status``, ``week``,
``account``, ``schema_version: journal/v1``). Publish follows TT
``report.py:654-657``: ``validate_week_manifest(m)``,
``assert_manifest_inputs_match(m, files)``,
``assert_claimed_lock_supported(m, files=files)``. A hand-written file
cannot unlock.
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
WEEK_MANIFEST_INPUTS = "Wochenmanifest hashed inputs fehlen"
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
    files: dict[str, Any] | None = None,
) -> Path:
    """Test helper. Writes TT schema; does **not** unlock publish without TT verify."""
    week = iso_week_id(day)
    path = Path(root) / WEEK_MANIFEST_NAME
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "week": week,
        "account": account,
        "lock_status": lock_status,
    }
    if files is not None:
        payload["files"] = files
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
    journal_dir: Path | None = None,
    positions_dir: Path | None = None,
    prepare_manifest: Path | None = None,
    accounts: Path | None = None,
) -> dict[str, Any]:
    """Publish only when TT JX8 accepts the manifest for this week+account."""
    from tradevidanalyser.tt_journal import (
        assert_claimed_lock_supported,
        assert_manifest_inputs_match,
        hashed_input_files,
        lock_source_paths,
        validate_week_manifest,
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
        validate_week_manifest(payload)
    except ImportError as exc:
        raise WeekManifestError(str(exc)) from exc
    except WeekManifestError:
        raise
    except Exception as exc:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}: {exc}") from exc
    try:
        files = hashed_input_files(
            payload,
            **lock_source_paths(
                root,
                journal_dir=journal_dir,
                positions_dir=positions_dir,
                prepare_manifest=prepare_manifest,
                accounts=accounts,
            ),
        )
    except ValueError as exc:
        raise WeekManifestError(str(exc) or WEEK_MANIFEST_INPUTS) from exc
    try:
        assert_manifest_inputs_match(payload, files)
        assert_claimed_lock_supported(payload, files)
    except ImportError as exc:
        raise WeekManifestError(str(exc)) from exc
    except WeekManifestError:
        raise
    except Exception as exc:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}: {exc}") from exc
    if payload.get("lock_status") != STATUS_LOCKED:
        raise WeekManifestError(f"{WEEK_MANIFEST_NOT_LOCKED}: {week}")
    return payload
