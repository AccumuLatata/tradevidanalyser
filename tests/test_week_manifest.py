from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

import pytest

from tradevidanalyser.tt_journal import (
    TT_LOCK_VALIDATE_MISSING,
    WEEK_MANIFEST_INPUT_MISSING,
    WEEK_MANIFEST_ROLE_UNKNOWN,
    hashed_input_files,
)
from tradevidanalyser.week_manifest import (
    SCHEMA_VERSION,
    WEEK_MANIFEST_MISSING,
    WEEK_MANIFEST_MISMATCH,
    WEEK_MANIFEST_INPUTS,
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
        match=f"{re.escape(TT_LOCK_VALIDATE_MISSING)}|{re.escape(WEEK_MANIFEST_NOT_LOCKED)}|{re.escape(WEEK_MANIFEST_INPUTS)}",
    ):
        require_locked_week_manifest(tmp_path, DAY, account="synth-amp")


def _role_sources(tmp_path: Path) -> dict[str, Path]:
    journal = tmp_path / "journal"
    positions = tmp_path / "positions"
    prepare_dir = tmp_path / "prepare"
    journal.mkdir()
    positions.mkdir()
    prepare_dir.mkdir()
    (journal / "journal_trades.parquet").write_bytes(b"j")
    (positions / "journal_positions.parquet").write_bytes(b"p")
    prepare = prepare_dir / "prepare_manifest.json"
    prepare.write_text("{}", encoding="utf-8")
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text("accounts: []\n", encoding="utf-8")
    return {
        "journal_dir": journal,
        "positions_dir": positions,
        "prepare_manifest": prepare,
        "accounts": accounts,
    }


def test_hashed_input_files_maps_roles(tmp_path: Path) -> None:
    sources = _role_sources(tmp_path)
    manifest = {
        "inputs": [
            {"role": "journal", "path": "journal_trades.parquet", "sha256": "aa"},
            {"role": "positions", "path": "journal_positions.parquet", "sha256": "bb"},
            {"role": "prepare", "path": "prepare_manifest.json", "sha256": "cc"},
            {"role": "accounts", "path": "accounts.yaml", "sha256": "dd"},
        ]
    }
    files = hashed_input_files(manifest, **sources)
    assert files["journal"] == sources["journal_dir"]
    assert files["positions"] == sources["positions_dir"]
    assert files["prepare"] == sources["prepare_manifest"]
    assert files["accounts"] == sources["accounts"]


def test_hashed_input_files_unknown_role_is_loud(tmp_path: Path) -> None:
    sources = _role_sources(tmp_path)
    manifest = {"inputs": [{"role": "ticks", "path": "ticks.parquet", "sha256": "00"}]}
    with pytest.raises(ValueError, match=WEEK_MANIFEST_ROLE_UNKNOWN):
        hashed_input_files(manifest, **sources)


def test_hashed_input_files_missing_path_is_loud(tmp_path: Path) -> None:
    sources = _role_sources(tmp_path)
    manifest = {"inputs": [{"role": "journal", "path": "missing.parquet", "sha256": "00"}]}
    with pytest.raises(ValueError, match=WEEK_MANIFEST_INPUT_MISSING):
        hashed_input_files(manifest, **sources)
