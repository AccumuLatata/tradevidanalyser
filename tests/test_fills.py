from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.fills import (
    FILL_RECORD_COLUMNS,
    FORBIDDEN_COST_COLUMNS,
    JOURNAL_TRADE_COLUMNS,
    TVA_FILL_COLUMNS,
    TVA_TRADE_COLUMNS,
    contract_fills_table,
    contract_trades_table,
    load_and_pair,
    thesistester_available,
    venue_from_hint,
)
from tradevidanalyser.fills_mirror import (
    load_tradesviz_executions,
    pair_journal_trades,
)
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, fills_session, transcribe_session
from tradevidanalyser.schema import RecordingInfo, SessionRecord
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "tradesviz_synthetic.csv"
EXPECTED_FILLS = FIXTURES / "expected_fills.parquet"
EXPECTED_TRADES = FIXTURES / "expected_trades.parquet"


def _session(
    root: Path,
    *,
    session_id: str = "2026-05-14_160000",
    start: str = "2026-05-14T16:00:00+02:00",
    duration_s: float = 3600.0,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-05-14 16-00-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=start,
            duration_s=duration_s,
            filename="2026-05-14 16-00-00.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def test_mirror_matches_committed_expected() -> None:
    fills = load_tradesviz_executions(SYNTHETIC, profile="tradesviz_executions")
    trades = pair_journal_trades(fills, include_manual=False)
    got_fills = contract_fills_table(fills)
    got_trades = contract_trades_table(trades)
    exp_fills = pq.read_table(EXPECTED_FILLS)
    exp_trades = pq.read_table(EXPECTED_TRADES)
    assert got_fills.column_names == exp_fills.column_names == list(FILL_RECORD_COLUMNS)
    assert got_trades.column_names == exp_trades.column_names == list(JOURNAL_TRADE_COLUMNS)
    assert got_fills.equals(exp_fills)
    assert got_trades.equals(exp_trades)


@pytest.mark.skipif(not thesistester_available(), reason="thesistester extra not installed")
def test_import_and_mirror_produce_identical_frames() -> None:
    m_loader, m_fills, m_trades = load_and_pair(SYNTHETIC, prefer_import=False)
    i_loader, i_fills, i_trades = load_and_pair(SYNTHETIC, prefer_import=True)
    assert m_loader == "mirror"
    assert i_loader == "import"
    assert contract_fills_table(m_fills).equals(contract_fills_table(i_fills))
    assert contract_trades_table(m_trades).equals(contract_trades_table(i_trades))


def test_commission_fees_discarded() -> None:
    header = SYNTHETIC.read_text(encoding="utf-8").splitlines()[0]
    assert "commission" in header
    assert "fees" in header
    fills = load_tradesviz_executions(SYNTHETIC, profile="tradesviz_executions")
    trades = pair_journal_trades(fills)
    for name in FORBIDDEN_COST_COLUMNS:
        assert name not in FILL_RECORD_COLUMNS
        assert name not in JOURNAL_TRADE_COLUMNS
        assert name not in TVA_FILL_COLUMNS
        assert name not in TVA_TRADE_COLUMNS
        assert name not in contract_fills_table(fills).column_names
        assert name not in contract_trades_table(trades).column_names


def test_venue_column_and_tva_trade_id(tva_root: Path) -> None:
    record = _session(tva_root)
    result = fills_session(
        record.id,
        root=tva_root,
        executions=SYNTHETIC,
        venue="topstepx",
    )
    assert result.status == "ok"
    assert result.fills == 4
    assert result.trades == 2
    assert result.venue == "topstepx"
    assert result.loader == "mirror"
    fills = pq.read_table(store.fills_path(tva_root, record.id))
    trades = pq.read_table(store.trades_path(tva_root, record.id))
    assert "venue" in fills.column_names
    assert set(fills.column("venue").to_pylist()) == {"topstepx"}
    assert "venue" in trades.column_names
    assert "tva_trade_id" in trades.column_names
    assert trades.column("tva_trade_id").to_pylist() == ["T01", "T02"]
    for name in FORBIDDEN_COST_COLUMNS:
        assert name not in fills.column_names
        assert name not in trades.column_names
    status = store.compute_status(tva_root, record.id)
    assert status.stages["fills"] == "ok"


def test_window_filter_excludes_other_days(tva_root: Path) -> None:
    record = _session(tva_root)
    all_fills = load_tradesviz_executions(SYNTHETIC, profile="tradesviz_executions")
    assert len(all_fills) == 12
    result = fills_session(record.id, root=tva_root, executions=SYNTHETIC, venue="amp")
    assert result.fills == 4
    stamps = pq.read_table(store.fills_path(tva_root, record.id)).column("timestamp").to_pylist()
    for ts in stamps:
        instant = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        assert instant.date().isoformat() == "2026-05-14"


def test_zero_fills_omits_stage_not_error(
    tva_root: Path, sample_video: Path, capsys
) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert main(
        [
            "--root",
            str(tva_root),
            "fills",
            record.id,
            "--executions",
            str(SYNTHETIC),
            "--venue",
            "amp",
        ]
    ) == 0
    out = capsys.readouterr().out
    assert '"status": "skipped"' in out
    assert "no fills in session window" in out
    assert not store.fills_path(tva_root, record.id).is_file()
    status = store.compute_status(tva_root, record.id)
    assert "fills" not in status.stages
    assert status.error is None
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_fills_failed_is_omitted(tva_root: Path) -> None:
    record = _session(
        tva_root,
        session_id="2026-09-11_143000",
        start="2026-09-11T14:30:00+02:00",
    )
    store.compute_status(tva_root, record.id, failed="fills", error="fills exploded")
    status = store.compute_status(tva_root, record.id)
    assert "fills" not in status.stages
    assert status.error is None


def test_filename_venue_hint() -> None:
    assert venue_from_hint(Path("amp_day.csv"), None) == "amp"
    assert venue_from_hint(Path("topstepx_export.csv"), None) == "topstepx"
    assert venue_from_hint(Path("executions.csv"), None) == "unknown"
    assert venue_from_hint(Path("amp_day.csv"), "topstepx") == "topstepx"


def test_fills_refuse_path_escape(tva_root: Path) -> None:
    assert (
        main(
            [
                "--root",
                str(tva_root),
                "fills",
                "../outside",
                "--executions",
                str(SYNTHETIC),
            ]
        )
        == 1
    )


def test_fills_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "fills" not in ALLOWED_RUN_STAGES


def test_fills_rerun_byte_identical(tva_root: Path) -> None:
    record = _session(tva_root)
    fills_session(record.id, root=tva_root, executions=SYNTHETIC, venue="amp")
    first = store.fills_path(tva_root, record.id).read_bytes()
    trades = store.trades_path(tva_root, record.id).read_bytes()
    fills_session(record.id, root=tva_root, executions=SYNTHETIC, venue="amp")
    assert store.fills_path(tva_root, record.id).read_bytes() == first
    assert store.trades_path(tva_root, record.id).read_bytes() == trades


def test_doctor_journal_warns_without_extra(tva_root: Path) -> None:
    ids = {c.id: c for c in run_doctor(tva_root).checks}
    assert ids["journal"].status in {"ok", "warn"}
    if not thesistester_available():
        assert ids["journal"].status == "warn"


def test_no_broker_order_endpoints() -> None:
    src = Path(__file__).resolve().parents[1] / "src"
    banned = re.compile(r"placeOrder|/api/Order|cancelOrder")
    hits: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        if banned.search(text):
            hits.append(str(path.relative_to(src)))
    assert hits == []


def test_eth_session_date_rolls_after_1800() -> None:
    fills = load_tradesviz_executions(SYNTHETIC, profile="tradesviz_executions")
    eth = next(fill for fill in fills if fill.source_group_id == "eth1805")
    assert eth.session_date.isoformat() == "2026-05-18"
    assert eth.timestamp == datetime(2026, 5, 17, 22, 5, tzinfo=timezone.utc)
