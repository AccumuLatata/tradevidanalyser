from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from tradevidanalyser.tt_journal import TT_LOCK_API_MISSING
from tradevidanalyser.week_manifest import (
    SCHEMA_VERSION,
    WEEK_MANIFEST_MISSING,
    WEEK_MANIFEST_MISMATCH,
    WEEK_MANIFEST_NOT_LOCKED,
    WeekManifestError,
    iso_week_id,
    require_locked_week_manifest,
    week_manifest_path,
    write_week_manifest,
)

DAY = date(2026, 9, 14)


def test_iso_week_and_lock_status_field(tmp_path: Path) -> None:
    path = write_week_manifest(tmp_path, DAY, lock_status="locked", account="synth-amp")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["lock_status"] == "locked"
    assert payload["week"] == "2026-W38"
    assert payload["account"] == "synth-amp"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "status" not in payload
    assert iso_week_id(DAY) == "2026-W38"


def test_legacy_weeks_manifest_is_not_read(tmp_path: Path) -> None:
    week = iso_week_id(DAY)
    legacy = tmp_path / "weeks" / week / "manifest.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "week": week,
                "account": "synth-amp",
                "lock_status": "locked",
            }
        ),
        encoding="utf-8",
    )
    assert week_manifest_path(tmp_path).name == "week_manifest.json"
    assert not week_manifest_path(tmp_path).is_file()
    with pytest.raises(WeekManifestError, match=WEEK_MANIFEST_MISSING):
        require_locked_week_manifest(tmp_path, DAY, account="synth-amp")


def test_week_or_account_mismatch_is_loud(tmp_path: Path) -> None:
    write_week_manifest(tmp_path, DAY, lock_status="locked", account="synth-amp")
    with pytest.raises(WeekManifestError, match=WEEK_MANIFEST_MISMATCH):
        require_locked_week_manifest(tmp_path, date(2026, 9, 21), account="synth-amp")
    with pytest.raises(WeekManifestError, match=WEEK_MANIFEST_MISMATCH):
        require_locked_week_manifest(tmp_path, DAY, account="synth-topstepx")
    with pytest.raises(WeekManifestError, match=WEEK_MANIFEST_MISMATCH):
        require_locked_week_manifest(tmp_path, DAY, account=None)


def test_handwritten_locked_file_still_needs_tt_verify(tmp_path: Path) -> None:
    write_week_manifest(tmp_path, DAY, lock_status="locked", account="synth-amp")
    with pytest.raises(
        WeekManifestError,
        match=f"{re.escape(TT_LOCK_API_MISSING)}|{re.escape(WEEK_MANIFEST_NOT_LOCKED)}",
    ):
        require_locked_week_manifest(tmp_path, DAY, account="synth-amp")
