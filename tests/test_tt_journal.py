"""[journal] tests that call ThesisTester public APIs. skipif when TT is absent.

Calls only the named signatures:
``validate_week_manifest(m)``, ``assert_manifest_inputs_match(m, files)``,
``assert_claimed_lock_supported(m, files=files)``,
``build_journal_positions(trades, lot_sources)``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradevidanalyser.fills import load_and_pair, thesistester_available
from tradevidanalyser.fills_mirror import NO_COST_SOURCE_ERROR, JournalIngestError
from tradevidanalyser.flags import ENV_NET_FEES
from tradevidanalyser.rules import RuleTrade, RulesConfig, evaluate_rules
from tradevidanalyser.tt_journal import (
    LOCK_MODULE,
    POSITIONS_MODULE,
    RECONCILE_MODULE,
    TOPSTEPX_MODULE,
    assert_claimed_lock_supported,
    assert_manifest_inputs_match,
    build_journal_positions,
    hashed_input_files,
    require_tt_symbol,
    validate_week_manifest,
)
from tradevidanalyser.week_manifest import (
    WEEK_MANIFEST_INPUTS,
    WEEK_MANIFEST_NOT_LOCKED,
    WeekManifestError,
    iso_week_id,
    require_locked_week_manifest,
    write_week_manifest,
)

pytestmark = pytest.mark.skipif(
    not thesistester_available(), reason="thesistester extra not installed"
)

DAY = date(2026, 9, 14)
T0 = datetime(2026, 9, 14, 7, 5, tzinfo=timezone.utc)


def test_named_tt_symbols_import() -> None:
    require_tt_symbol(LOCK_MODULE, "validate_week_manifest")
    require_tt_symbol(LOCK_MODULE, "assert_manifest_inputs_match")
    require_tt_symbol(LOCK_MODULE, "assert_claimed_lock_supported")
    require_tt_symbol(POSITIONS_MODULE, "build_journal_positions")
    require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    require_tt_symbol(TOPSTEPX_MODULE, "write_topstepx_artifacts")
    require_tt_symbol(RECONCILE_MODULE, "reconcile_files")


def test_handwritten_locked_is_refused_by_tt_lock(tmp_path: Path) -> None:
    write_week_manifest(tmp_path, DAY, lock_status="locked", account="synth-amp")
    with pytest.raises(
        WeekManifestError,
        match=f"{WEEK_MANIFEST_NOT_LOCKED}|{WEEK_MANIFEST_INPUTS}",
    ):
        require_locked_week_manifest(tmp_path, DAY, account="synth-amp")


def test_tampered_input_hash_is_refused_by_jx8(tmp_path: Path) -> None:
    (tmp_path / "journal_trades.parquet").write_bytes(b"tampered")
    manifest = {
        "schema_version": "journal/v1",
        "week": iso_week_id(DAY),
        "account": "synth-amp",
        "lock_status": "locked",
        "files": {"journal_trades.parquet": "0" * 64},
    }
    (tmp_path / "week_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    files = hashed_input_files(manifest, tmp_path)
    with pytest.raises(Exception):
        validate_week_manifest(manifest)
        assert_manifest_inputs_match(manifest, files)
        assert_claimed_lock_supported(manifest, files=files)


def test_r_max10_counts_jx2_positions(monkeypatch: pytest.MonkeyPatch) -> None:
    import pandas as pd

    monkeypatch.setenv(ENV_NET_FEES, "1")
    trades = pd.DataFrame(
        {
            "trade_id": ["jt:a:0", "jt:a:1", "jt:b:0"],
            "account": ["synth-amp", "synth-amp", "synth-amp"],
            "instrument": ["MNQ", "MNQ", "MNQ"],
            "qty": [1, 1, 1],
            "entry_timestamp": [T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=80)],
            "exit_timestamp": [
                T0 + timedelta(seconds=5),
                T0 + timedelta(seconds=15),
                T0 + timedelta(seconds=90),
            ],
        }
    )
    lots = pd.DataFrame(
        {
            "trade_id": ["jt:a:0", "jt:a:1", "jt:b:0"],
            "account": ["synth-amp", "synth-amp", "synth-amp"],
            "instrument": ["MNQ", "MNQ", "MNQ"],
            "qty": [1, 1, 1],
            "timestamp": [T0, T0 + timedelta(seconds=10), T0 + timedelta(seconds=80)],
        }
    )
    positions = build_journal_positions(trades, lots)
    raw = positions.to_dict(orient="records") if hasattr(positions, "to_dict") else list(positions)
    ids: list[str] = []
    for item in raw:
        if isinstance(item, dict):
            pid = item.get("position_id") or item.get("id")
        else:
            pid = getattr(item, "position_id", None) or getattr(item, "id", None)
        if pid:
            ids.append(str(pid))
    ids = list(dict.fromkeys(ids))
    assert ids, "build_journal_positions(trades, lot_sources) must return positions"
    rule_trades = [
        RuleTrade(
            tva_trade_id=f"T{index:02d}",
            instrument="MNQ",
            direction="long",
            entry_timestamp=T0 + timedelta(seconds=index),
            exit_timestamp=T0 + timedelta(seconds=index + 1),
            entry_price=21000.0,
            exit_price=21001.0,
            stop_price=None,
            hold_seconds=1.0,
            net_pnl_currency=1.0,
            gross_pnl_currency=1.0,
            status="closed",
            account="synth-amp",
            position_id=pid,
        )
        for index, pid in enumerate(ids, start=1)
    ]
    check = {item.rule: item for item in evaluate_rules(rule_trades, RulesConfig())}["R-MAX10"]
    assert check.evidence["count_basis"] == "positions"
    assert check.evidence["count"] == len(ids)


def test_flag_on_tv_csv_stays_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NET_FEES, "1")
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    with pytest.raises(JournalIngestError, match=NO_COST_SOURCE_ERROR):
        load_and_pair(csv, prefer_import=True)
    with pytest.raises(JournalIngestError, match=NO_COST_SOURCE_ERROR):
        load_and_pair(csv, prefer_import=False)
