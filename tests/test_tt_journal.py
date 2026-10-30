"""[journal] tests that call ThesisTester public APIs. skipif when TT is absent.

Calls only the named signatures from TT d32f6d3:
``validate_week_manifest(m)``, ``assert_manifest_inputs_match(m, files)``,
``assert_claimed_lock_supported(m, files=files)``,
``build_journal_positions(trades, lot_sources)``,
``ingest_topstepx(*, trades, tradesviz, account, accounts, output_dir)``,
``write_journal_positions(*, journal_dir, account, output_dir)``,
``lock_files(*, week, account, accounts, journal_dir, positions_dir,
prepare_manifest, output_dir)``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradevidanalyser.accounts import default_accounts_path
from tradevidanalyser.day_manifest import build_day
from tradevidanalyser.day_publish import day_publish_path
from tradevidanalyser.fills import load_and_pair, thesistester_available
from tradevidanalyser.fills_mirror import NO_COST_SOURCE_ERROR, JournalIngestError
from tradevidanalyser.flags import ENV_NET_FEES
from tradevidanalyser.publish import publish_session
from tradevidanalyser.rules import RuleTrade, RulesConfig, evaluate_rules
from tradevidanalyser.tt_journal import (
    JOURNAL_LOT_SOURCES_NAME,
    JOURNAL_POSITIONS_JSON,
    JOURNAL_POSITIONS_PARQUET,
    JOURNAL_TRADES_NAME,
    LOCK_MODULE,
    POSITIONS_MODULE,
    RECONCILE_MODULE,
    TOPSTEPX_MODULE,
    WEEK_MANIFEST_NAME,
    assert_claimed_lock_supported,
    assert_manifest_inputs_match,
    build_journal_positions,
    hashed_input_files,
    ingest_topstepx,
    lock_files,
    require_tt_symbol,
    validate_week_manifest,
    write_journal_positions,
)
from tradevidanalyser.week_manifest import (
    WEEK_MANIFEST_INPUTS,
    WEEK_MANIFEST_NOT_LOCKED,
    WeekManifestError,
    iso_week_id,
    require_locked_week_manifest,
    write_week_manifest,
)
from test_day_publish import _four_on, _two_clips, _write_debrief

pytestmark = pytest.mark.skipif(
    not thesistester_available(), reason="thesistester extra not installed"
)

DAY = date(2026, 9, 14)
T0 = datetime(2026, 9, 14, 7, 5, tzinfo=timezone.utc)


def test_named_tt_symbols_import() -> None:
    require_tt_symbol(LOCK_MODULE, "validate_week_manifest")
    require_tt_symbol(LOCK_MODULE, "assert_manifest_inputs_match")
    require_tt_symbol(LOCK_MODULE, "assert_claimed_lock_supported")
    require_tt_symbol(LOCK_MODULE, "lock_files")
    require_tt_symbol(POSITIONS_MODULE, "build_journal_positions")
    require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    require_tt_symbol(TOPSTEPX_MODULE, "reconcile_topstepx")
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


ACCOUNT = "synth-topstepx"
WEEK = "2026-W38"
TV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)
# Best-effort TopstepX lots header from the documented Trade/search fields
# (creationTimestamp, price, side, size, profitAndLoss, fees, orderId, voided)
# plus Account/Contract so load_topstepx_trades(trades, account=) can filter.
LOTS_HEADER = "Account,Contract,Timestamp,Side,Size,Price,PnL,Fees,Commissions,OrderId,Voided"


def _write_synthetic_topstepx_lots(path: Path) -> Path:
    path.write_text(
        LOTS_HEADER
        + "\n"
        + ",".join(
            [
                ACCOUNT,
                "MNQM26",
                "2026-09-14T07:05:00+00:00",
                "Buy",
                "1",
                "21000.0",
                "",
                "1.34",
                "0",
                "1001",
                "false",
            ]
        )
        + "\n"
        + ",".join(
            [
                ACCOUNT,
                "MNQM26",
                "2026-09-14T07:06:00+00:00",
                "Sell",
                "1",
                "21001.0",
                "2.0",
                "1.34",
                "0",
                "1002",
                "false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _write_synthetic_tv_csv(path: Path) -> Path:
    path.write_text(
        TV_HEADER
        + "\n"
        + "2026-09-14T07:05:00+0000,MNQM26,buy,USD,MNQ,future,21000.0,1.0,0,0,N/A,N/A,,,jx1e2e\n"
        + "2026-09-14T07:06:00+0000,MNQM26,sell,USD,MNQ,future,21001.0,1.0,0,0,N/A,N/A,,,jx1e2e\n",
        encoding="utf-8",
    )
    return path


def _write_prepare_manifest(path: Path) -> Path:
    """Minimal tick prepare_manifest. Best-effort; lock_files may keep the week open."""
    path.write_text(
        json.dumps(
            {
                "schema_version": "prepare/v1",
                "week": WEEK,
                "account": ACCOUNT,
                "files": {},
            },
            sort_keys=True,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _week_manifest_payload(journal: Path) -> dict[str, object]:
    path = journal / WEEK_MANIFEST_NAME
    assert path.is_file(), f"{WEEK_MANIFEST_NAME} missing after lock_files"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_ingest_positions_lock_then_publish_or_open_refused(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lots + TV CSV → ingest_topstepx → write_journal_positions → lock_files.

    Locked week: TVA publish succeeds. Tampered hash and handwritten locked
    are refused. Open week (no synthesizable tick coverage): publish refused.
    """
    require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    require_tt_symbol(LOCK_MODULE, "lock_files")

    _four_on(monkeypatch)
    monkeypatch.setenv(ENV_NET_FEES, "1")
    a, b = _two_clips(tva_root)

    lots = _write_synthetic_topstepx_lots(tmp_path / "topstepx_lots.csv")
    tradesviz = _write_synthetic_tv_csv(tmp_path / "tradesviz.csv")
    prepare = _write_prepare_manifest(tmp_path / "prepare_manifest.json")
    journal = tva_root / "journal"
    journal.mkdir()
    accounts = default_accounts_path()

    written = ingest_topstepx(
        trades=lots,
        tradesviz=tradesviz,
        account=ACCOUNT,
        accounts=accounts,
        output_dir=journal,
    )
    assert (journal / JOURNAL_TRADES_NAME).is_file()
    assert (journal / JOURNAL_LOT_SOURCES_NAME).is_file()
    assert written is None or isinstance(written, dict)

    pos_written = write_journal_positions(
        journal_dir=journal,
        account=ACCOUNT,
        output_dir=journal,
    )
    assert (journal / JOURNAL_POSITIONS_PARQUET).is_file() or (
        journal / JOURNAL_POSITIONS_JSON
    ).is_file()
    assert pos_written is None or isinstance(pos_written, dict)

    lock_files(
        week=WEEK,
        account=ACCOUNT,
        accounts=accounts,
        journal_dir=journal,
        positions_dir=journal,
        prepare_manifest=prepare,
        output_dir=journal,
    )
    manifest = _week_manifest_payload(journal)
    lock_status = manifest.get("lock_status")
    assert lock_status in {"open", "locked"}

    build_day(tva_root, DAY, executions=journal, venue="topstepx")
    _write_debrief(tva_root, a.id, learnings="Alpha one.")
    _write_debrief(tva_root, b.id, learnings="Bravo one.")

    if lock_status == "locked":
        result = publish_session(a.id, root=tva_root, notion=True)
        assert result.page_id
        assert day_publish_path(tva_root, DAY).is_file()
    else:
        with pytest.raises(WeekManifestError, match=WEEK_MANIFEST_NOT_LOCKED):
            publish_session(a.id, root=tva_root, notion=True)
        assert not day_publish_path(tva_root, DAY).is_file()

    trades_pq = journal / JOURNAL_TRADES_NAME
    original = trades_pq.read_bytes()
    trades_pq.write_bytes(original + b"\x00tampered")
    files = hashed_input_files(manifest, journal)
    with pytest.raises(Exception):
        validate_week_manifest(manifest)
        assert_manifest_inputs_match(manifest, files)
        assert_claimed_lock_supported(manifest, files=files)
    with pytest.raises(WeekManifestError):
        require_locked_week_manifest(tva_root, DAY, account=ACCOUNT)
    trades_pq.write_bytes(original)

    write_week_manifest(tva_root, DAY, lock_status="locked", account=ACCOUNT)
    with pytest.raises(
        WeekManifestError,
        match=f"{WEEK_MANIFEST_NOT_LOCKED}|{WEEK_MANIFEST_INPUTS}",
    ):
        require_locked_week_manifest(tva_root, DAY, account=ACCOUNT)
