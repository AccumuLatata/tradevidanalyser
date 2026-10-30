"""[journal] tests that call ThesisTester public APIs. skipif when TT is absent.

Calls only the named signatures from TT d32f6d3:
``validate_week_manifest(m)``, ``assert_manifest_inputs_match(m, files)``,
``assert_claimed_lock_supported(m, files=files)``,
``ingest_topstepx(*, trades, tradesviz, account, accounts, output_dir)``,
``write_journal_positions(*, journal_dir, account, output_dir)``,
``lock_files(*, week, account, accounts, journal_dir, positions_dir,
prepare_manifest, output_dir)``.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from tradevidanalyser.accounts import default_accounts_path
from tradevidanalyser.day_manifest import build_day
from tradevidanalyser.day_publish import day_publish_path
from tradevidanalyser.fills import load_and_pair, load_tt_journal_dir, thesistester_available
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
    hashed_input_files,
    ingest_topstepx,
    lock_files,
    lock_source_paths,
    require_tt_symbol,
    validate_week_manifest,
    write_journal_positions,
)
from tradevidanalyser.week_manifest import (
    WEEK_MANIFEST_INPUTS,
    WEEK_MANIFEST_NOT_LOCKED,
    WeekManifestError,
    require_locked_week_manifest,
    write_week_manifest,
)
from test_day_publish import _four_on, _two_clips, _write_debrief

pytestmark = pytest.mark.skipif(
    not thesistester_available(), reason="thesistester extra not installed"
)

DAY = date(2026, 9, 14)
ACCOUNT = "synth-topstepx"
WEEK = "2026-W38"
TV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)
LOTS_HEADER = (
    "Id,ContractName,EnteredAt,ExitedAt,EntryPrice,ExitPrice,Fees,PnL,"
    "Size,Type,TradeDay,TradeDuration,Commissions"
)
LOTS_ROW = (
    "s1,MNQU6,09/14/2026 07:05:00 +00:00,09/14/2026 07:06:00 +00:00,"
    "21000.00,21001.00,1.34,2.00,1,Long,09/14/2026 00:00:00 -05:00,60,0.00"
)
PREPARE_MANIFEST = {"schema_version": "prepare/v1", "sessions": [], "contract": "MNQU6"}


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


def test_flag_on_tv_csv_stays_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NET_FEES, "1")
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    with pytest.raises(JournalIngestError, match=NO_COST_SOURCE_ERROR):
        load_and_pair(csv, prefer_import=True)
    with pytest.raises(JournalIngestError, match=NO_COST_SOURCE_ERROR):
        load_and_pair(csv, prefer_import=False)


def _write_synthetic_topstepx_lots(path: Path) -> Path:
    path.write_text(LOTS_HEADER + "\n" + LOTS_ROW + "\n", encoding="utf-8")
    return path


def _write_synthetic_tv_csv(path: Path) -> Path:
    path.write_text(
        TV_HEADER
        + "\n"
        + "2026-09-14T07:05:00+0000,MNQU26,buy,USD,MNQ,future,21000.0,1.0,0,0,N/A,N/A,,,s1\n"
        + "2026-09-14T07:06:00+0000,MNQU26,sell,USD,MNQ,future,21001.0,1.0,0,0,N/A,N/A,,,s1\n",
        encoding="utf-8",
    )
    return path


def _write_test_accounts_yaml(path: Path) -> Path:
    shipped = yaml.safe_load(default_accounts_path().read_text(encoding="utf-8"))
    accounts = []
    for entry in shipped["accounts"]:
        row = dict(entry)
        row["trading_days"] = ["2026-09-14"]
        accounts.append(row)
    path.write_text(yaml.safe_dump({"accounts": accounts}, sort_keys=False), encoding="utf-8")
    return path


def _write_prepare_and_ticks(prepare_dir: Path) -> Path:
    prepare_dir.mkdir(parents=True, exist_ok=True)
    prepare = prepare_dir / "prepare_manifest.json"
    prepare.write_text(
        json.dumps(PREPARE_MANIFEST, separators=(",", ":")),
        encoding="utf-8",
    )
    start = datetime(2026, 9, 14, 7, 5, tzinfo=timezone.utc)
    n = 61
    table = pa.table(
        {
            "timestamp": pa.array(
                [start + timedelta(seconds=index) for index in range(n)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "price": pa.array(
                [21000.0 + index / 60.0 for index in range(n)],
                type=pa.float64(),
            ),
        }
    )
    pq.write_table(table, prepare_dir / "ticks.parquet")
    return prepare


def _lock_sources(root: Path) -> dict[str, Path]:
    return lock_source_paths(root)


def _ingest_and_positions(root: Path, tmp_path: Path) -> dict[str, Path]:
    journal = root / "journal"
    positions = root / "positions"
    journal.mkdir()
    positions.mkdir()
    lots = _write_synthetic_topstepx_lots(tmp_path / "topstepx_lots.csv")
    tradesviz = _write_synthetic_tv_csv(tmp_path / "tradesviz.csv")
    accounts = _write_test_accounts_yaml(root / "accounts.yaml")
    prepare = _write_prepare_and_ticks(root / "prepare")
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
        output_dir=positions,
    )
    assert (positions / JOURNAL_POSITIONS_PARQUET).is_file()
    assert (positions / JOURNAL_POSITIONS_JSON).is_file()
    assert pos_written is None or isinstance(pos_written, dict)
    return {
        "journal": journal,
        "positions": positions,
        "prepare": prepare,
        "accounts": accounts,
    }


def _ingest_and_lock(root: Path, tmp_path: Path) -> dict[str, Path]:
    paths = _ingest_and_positions(root, tmp_path)
    lock_files(
        week=WEEK,
        account=ACCOUNT,
        accounts=paths["accounts"],
        journal_dir=paths["journal"],
        positions_dir=paths["positions"],
        prepare_manifest=paths["prepare"],
        output_dir=root,
    )
    return paths


def _week_manifest_payload(root: Path) -> dict[str, object]:
    path = root / WEEK_MANIFEST_NAME
    assert path.is_file(), f"{WEEK_MANIFEST_NAME} missing after lock_files"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_r_max10_counts_jx2_positions(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_NET_FEES, "1")
    paths = _ingest_and_positions(tva_root, tmp_path)
    _fills, trades = load_tt_journal_dir(
        paths["journal"],
        positions_dir=paths["positions"],
        account=ACCOUNT,
    )
    assert trades, "ingest_topstepx must produce journal trades"
    ids = list(dict.fromkeys(trade.position_id for trade in trades if trade.position_id))
    assert ids, "write_journal_positions must stamp position_id"
    rule_trades = [
        RuleTrade(
            tva_trade_id=f"T{index:02d}",
            instrument="MNQ",
            direction="long",
            entry_timestamp=trade.entry_timestamp,
            exit_timestamp=trade.exit_timestamp or trade.entry_timestamp + timedelta(seconds=1),
            entry_price=trade.entry_price,
            exit_price=trade.exit_price or trade.entry_price,
            stop_price=None,
            hold_seconds=1.0,
            net_pnl_currency=trade.net_pnl_currency or 1.0,
            gross_pnl_currency=trade.gross_pnl_currency or 1.0,
            status="closed",
            account=trade.account,
            position_id=trade.position_id,
        )
        for index, trade in enumerate(trades, start=1)
    ]
    check = {item.rule: item for item in evaluate_rules(rule_trades, RulesConfig())}["R-MAX10"]
    assert check.evidence["count_basis"] == "positions"
    assert check.evidence["count"] == len(ids)


def test_ingest_positions_lock_then_publish(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """lots + TV CSV → ingest_topstepx → write_journal_positions → lock_files.

    TT returns ``lock_status: locked``. TVA publish succeeds. Tampered
    ``journal_trades.parquet`` is ``input hash mismatch``. A hand-edited
    ``locked`` is ``lock_status_inconsistent``.
    """
    require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    require_tt_symbol(LOCK_MODULE, "lock_files")

    _four_on(monkeypatch)
    monkeypatch.setenv(ENV_NET_FEES, "1")
    a, b = _two_clips(tva_root)
    paths = _ingest_and_lock(tva_root, tmp_path)
    manifest = _week_manifest_payload(tva_root)
    assert manifest.get("lock_status") == "locked"
    sources = _lock_sources(tva_root)

    build_day(
        tva_root,
        DAY,
        executions=paths["journal"],
        venue="topstepx",
        positions_dir=paths["positions"],
        account=ACCOUNT,
    )
    _write_debrief(tva_root, a.id, learnings="Alpha one.")
    _write_debrief(tva_root, b.id, learnings="Bravo one.")
    result = publish_session(a.id, root=tva_root, notion=True)
    assert result.page_id
    assert day_publish_path(tva_root, DAY).is_file()

    trades_pq = paths["journal"] / JOURNAL_TRADES_NAME
    original = trades_pq.read_bytes()
    trades_pq.write_bytes(original + b"\x00tampered")
    files = hashed_input_files(manifest, **sources)
    with pytest.raises(Exception, match="input hash mismatch"):
        assert_manifest_inputs_match(manifest, files)
    trades_pq.write_bytes(original)

    hand = json.loads(json.dumps(manifest))
    if isinstance(hand.get("days"), list):
        hand["days"] = []
    elif isinstance(hand.get("days"), dict):
        hand["days"] = {}
    elif isinstance(hand.get("positions"), list):
        hand["positions"] = []
    elif isinstance(hand.get("positions"), dict):
        hand["positions"] = {}
    else:
        hand["days"] = []
    (tva_root / WEEK_MANIFEST_NAME).write_text(json.dumps(hand, indent=2) + "\n", encoding="utf-8")
    hand_files = hashed_input_files(hand, **sources)
    validate_week_manifest(hand)
    assert_manifest_inputs_match(hand, hand_files)
    with pytest.raises(Exception, match="lock_status_inconsistent"):
        assert_claimed_lock_supported(hand, files=hand_files)
