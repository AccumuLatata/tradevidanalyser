from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.fills import (
    FillsError,
    RECON_STATUSES,
    attach_recon_status,
    load_reconcile_status_map,
    lookup_recon_status,
    trades_table,
)
from tradevidanalyser.fills_mirror import load_tradesviz_executions, pair_journal_trades
from tradevidanalyser.pipeline import fills_session
from tradevidanalyser.schema import RecordingInfo, SessionRecord

FIXTURES = Path(__file__).parent / "fixtures"
SYNTHETIC = FIXTURES / "tradesviz_synthetic.csv"


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


def _write_reconcile(path: Path, days: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": "journal/v1", "days": days}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def test_reconciled_attaches_on_matching_session_date(tva_root: Path, tmp_path: Path) -> None:
    record = _session(tva_root)
    recon = _write_reconcile(
        tmp_path / "journal" / "reconcile.json",
        [
            {
                "session_date": "2026-05-14",
                "instrument": "MNQ",
                "status": "reconciled",
                "journal_fill_count": 4,
                "amp_fill_count": 4,
                "journal_gross_usd": 1.5,
                "amp_ps_usd": 1.5,
                "fee_total_usd": 0.0,
                "day_fees_extra": 0.0,
                "note": "",
            }
        ],
    )
    result = fills_session(
        record.id,
        root=tva_root,
        executions=SYNTHETIC,
        venue="amp",
        reconcile_dir=recon.parent,
    )
    assert result.status == "ok"
    assert result.recon_days == 1
    assert result.recon_attached == result.trades == 2
    trades = pq.read_table(store.trades_path(tva_root, record.id))
    assert "recon_status" in trades.column_names
    assert set(trades.column("recon_status").to_pylist()) == {"reconciled"}


def test_cli_reconcile_dir(tva_root: Path, tmp_path: Path, capsys) -> None:
    record = _session(tva_root)
    recon = _write_reconcile(
        tmp_path / "reconcile.json",
        [{"session_date": "2026-05-14", "instrument": "MNQ", "status": "reconciled"}],
    )
    assert (
        main(
            [
                "--root",
                str(tva_root),
                "fills",
                record.id,
                "--executions",
                str(SYNTHETIC),
                "--venue",
                "amp",
                "--reconcile-dir",
                str(recon),
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["recon_attached"] == 2
    statuses = pq.read_table(store.trades_path(tva_root, record.id)).column("recon_status")
    assert statuses.to_pylist() == ["reconciled", "reconciled"]


def test_recon_status_by_instrument(tmp_path: Path) -> None:
    path = _write_reconcile(
        tmp_path / "reconcile.json",
        [
            {"session_date": "2026-06-12", "instrument": "MES", "status": "reconciled"},
            {"session_date": "2026-06-12", "instrument": "MNQ", "status": "pnl_mismatch"},
        ],
    )
    mapping = load_reconcile_status_map(path)
    assert lookup_recon_status(date(2026, 6, 12), "MES", mapping) == "reconciled"
    assert lookup_recon_status(date(2026, 6, 12), "MNQ", mapping) == "pnl_mismatch"
    assert lookup_recon_status(date(2026, 6, 13), "MES", mapping) is None


def test_unmatched_day_is_null(tva_root: Path, tmp_path: Path) -> None:
    record = _session(tva_root)
    recon = _write_reconcile(
        tmp_path / "reconcile.json",
        [{"session_date": "2026-01-01", "instrument": "MNQ", "status": "amp_missing"}],
    )
    fills_session(
        record.id,
        root=tva_root,
        executions=SYNTHETIC,
        venue="amp",
        reconcile_dir=recon,
    )
    statuses = pq.read_table(store.trades_path(tva_root, record.id)).column("recon_status")
    assert statuses.to_pylist() == [None, None]


def test_without_reconcile_dir_omits_column(tva_root: Path) -> None:
    record = _session(tva_root)
    fills_session(record.id, root=tva_root, executions=SYNTHETIC, venue="amp")
    trades = pq.read_table(store.trades_path(tva_root, record.id))
    assert "recon_status" not in trades.column_names


def test_missing_reconcile_json_errors(tva_root: Path, tmp_path: Path) -> None:
    record = _session(tva_root)
    with pytest.raises(FillsError, match="reconcile.json"):
        fills_session(
            record.id,
            root=tva_root,
            executions=SYNTHETIC,
            venue="amp",
            reconcile_dir=tmp_path / "empty",
        )


def test_unknown_status_fails_closed(tmp_path: Path) -> None:
    path = _write_reconcile(
        tmp_path / "reconcile.json",
        [{"session_date": "2026-05-14", "instrument": "MNQ", "status": "pretty_close"}],
    )
    with pytest.raises(FillsError, match="status"):
        load_reconcile_status_map(path)


def test_attach_does_not_parse_pdf(tmp_path: Path) -> None:
    fills = load_tradesviz_executions(SYNTHETIC, profile="tradesviz_executions")
    trades = pair_journal_trades(fills, include_manual=False)
    table = trades_table(trades, venue="amp")
    mapping = {(date(2026, 5, 14), "MNQ"): "reconciled"}
    attached = attach_recon_status(table, mapping)
    may14 = [
        status
        for day, inst, status in zip(
            attached.column("session_date").to_pylist(),
            attached.column("instrument").to_pylist(),
            attached.column("recon_status").to_pylist(),
            strict=True,
        )
        if str(day) == "2026-05-14" and inst == "MNQ"
    ]
    assert may14
    assert set(may14) == {"reconciled"}
    assert RECON_STATUSES == {
        "reconciled",
        "journal_missing",
        "amp_missing",
        "multiset_mismatch",
        "pnl_mismatch",
    }
    src = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "fills.py"
    text = src.read_text(encoding="utf-8")
    assert "amp_statement" not in text
    assert "pypdf" not in text
    assert "load_amp_statement" not in text
