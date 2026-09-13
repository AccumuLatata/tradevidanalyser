from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ledger import (
    add_session,
    build_summary,
    ledger_db_path,
    ledger_summary,
    rollup,
    session_recorded,
    stated_lab_agree,
)
from tradevidanalyser.pipeline import extract_session, fills_session, transcribe_session
from tradevidanalyser.schema import (
    Insights,
    RecordingInfo,
    SessionEvent,
    SessionRecord,
)
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app


def _session(root: Path, session_id: str, *, duration_s: float = 7200.0) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{session_id.replace('_', ' ')}.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=f"{session_id[:10]}T14:30:00+02:00",
            duration_s=duration_s,
            filename=f"{session_id.replace('_', ' ')}.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_trades(root: Path, session_id: str) -> None:
    table = pa.table(
        {
            "tva_trade_id": ["T01", "T02"],
            "trade_id": ["jt-1", "jt-2"],
            "entry_fill_id": ["fill-a", "fill-b"],
            "direction": ["long", "short"],
            "instrument": ["MNQ", "MNQ"],
            "entry_price": [21000.0, 21010.0],
            "exit_price": [20990.0, 21020.0],
            "net_pnl_currency": [-10.0, 10.0],
            "status": ["closed", "closed"],
            "venue": ["amp", "amp"],
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _write_rules(root: Path, session_id: str) -> None:
    store.write_json(
        store.rules_path(root, session_id),
        {
            "schema_version": "1",
            "session_id": session_id,
            "rules": [
                {"rule": "R-MAX10", "status": "pass", "evidence": {}, "reason": None},
                {"rule": "R-PLAYBOOK", "status": "violated", "evidence": {}, "reason": None},
            ],
        },
    )


def _write_stated_lab(root: Path, session_id: str) -> None:
    store.write_json(
        store.evidence_path(root, session_id),
        {
            "schema_version": "1",
            "provider": "fake",
            "model": "none",
            "prompt_version": "stated-keyword-v1",
            "session_id": session_id,
            "trades": [
                {
                    "tva_trade_id": "T01",
                    "window": {"t0": 0.0, "t1": 60.0},
                    "stated": {"setup": {"value": "ONH", "seg": "seg_001"}},
                    "alignment_confidence": 1.0,
                },
                {
                    "tva_trade_id": "T02",
                    "window": {"t0": 120.0, "t1": 180.0},
                    "stated": {"setup": {"value": "ONH", "seg": "seg_002"}},
                    "alignment_confidence": 1.0,
                },
            ],
        },
    )
    store.write_json(
        store.context_path(root, session_id),
        {
            "schema_version": "1",
            "session_id": session_id,
            "lab": {
                "per_trade": {
                    "T01": {"nearest_level_token": "ONH", "tag_alignment": "all_aligned"},
                    "T02": {"nearest_level_token": "pdPOC", "tag_alignment": "conflict"},
                }
            },
            "gaps": [],
        },
    )


def _write_events(root: Path, session_id: str) -> None:
    store.save_insights(
        root,
        session_id,
        Insights(
            provider="fake",
            model="none",
            session_events=[
                SessionEvent(t=600.0, kind="hourly_checkin", seg="seg_001", text="stunde")
            ],
        ),
    )


def _seed(root: Path, session_id: str) -> SessionRecord:
    record = _session(root, session_id)
    _write_trades(root, session_id)
    _write_rules(root, session_id)
    _write_stated_lab(root, session_id)
    _write_events(root, session_id)
    return record


TEN_IDS = [
    "2026-09-07_143000",
    "2026-09-08_143000",
    "2026-09-09_143000",
    "2026-09-10_143000",
    "2026-09-11_143000",
    "2026-09-14_143000",
    "2026-09-15_143000",
    "2026-09-16_143000",
    "2026-09-17_143000",
    "2026-09-18_143000",
]


def _seed_ten(root: Path) -> None:
    for session_id in TEN_IDS:
        _seed(root, session_id)
        add_session(session_id, root=root)


def test_stated_lab_agree_helpers() -> None:
    assert stated_lab_agree("ONH", None, "ONH", None) is True
    assert stated_lab_agree("ONH", None, "pdPOC", None) is False
    assert stated_lab_agree(None, None, None, "all_aligned") is True
    assert stated_lab_agree(None, None, None, None) is None


def test_add_is_idempotent(tva_root: Path) -> None:
    record = _seed(tva_root, "2026-09-11_143000")
    assert "ledger" not in store.compute_status(tva_root, record.id).stages
    first = add_session(record.id, root=tva_root)
    second = add_session(record.id, root=tva_root)
    assert first.status == second.status == "ok"
    assert first.as_dict() == second.as_dict()
    assert first.trades == 2
    assert first.rules == 2
    assert first.events == 1
    summary = ledger_summary(tva_root, weeks=4)
    assert summary.sessions == 1
    assert summary.trades == 2
    assert session_recorded(tva_root, record.id)
    assert store.compute_status(tva_root, record.id).stages["ledger"] == "ok"


def test_ten_sessions_weekly_rollup_math(tva_root: Path) -> None:
    _seed_ten(tva_root)
    summary = ledger_summary(tva_root, weeks=4)
    assert summary.sessions == 10
    assert summary.trades == 20
    assert summary.hours == 20.0
    assert summary.trades_per_hour == 1.0
    assert summary.adherence.passed == 10
    assert summary.adherence.violated == 10
    assert summary.adherence.rate == 0.5
    assert summary.violations.total == 10
    assert summary.violations.by_rule["R-PLAYBOOK"] == 10
    assert summary.stated_vs_lab.agree == 10
    assert summary.stated_vs_lab.disagree == 10
    assert summary.stated_vs_lab.rate == 0.5
    week_ids = [period.id for period in summary.periods]
    assert week_ids == ["2026-W37", "2026-W38"]
    w37 = next(period for period in summary.periods if period.id == "2026-W37")
    assert w37.sessions == 5
    assert w37.trades == 10
    assert w37.trades_per_hour == 1.0
    one = ledger_summary(tva_root, weeks=1)
    assert one.sessions == 5
    assert one.periods[0].id == "2026-W38"
    markdown = rollup(tva_root, week="2026-W37").markdown
    assert markdown.startswith("# Ledger rollup")
    assert "Week 2026-W37" in markdown
    assert "Sessions: 5" in markdown
    assert "Trades/hour: 1.0" in markdown
    assert "R-PLAYBOOK" in markdown
    both = rollup(tva_root, week="2026-W37", month="2026-09")
    assert "Week 2026-W37" in both.markdown
    assert "Month 2026-09" in both.markdown


def test_cli_ledger_add_and_rollup(tva_root: Path, capsys) -> None:
    record = _seed(tva_root, "2026-09-11_143000")
    assert main(["--root", str(tva_root), "ledger", "add", record.id]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    assert main(["--root", str(tva_root), "rollup", "--week"]) == 0
    text = capsys.readouterr().out
    assert "# Ledger rollup" in text
    assert "Week 2026-W37" in text
    assert main(["--root", str(tva_root), "--json", "rollup", "--week"]) == 0
    payload = capsys.readouterr().out
    assert '"markdown"' in payload
    assert '"sessions"' in payload


def test_parquet_prices_land_in_duckdb(tva_root: Path) -> None:
    record = _seed(tva_root, "2026-09-11_143000")
    add_session(record.id, root=tva_root)
    import duckdb

    con = duckdb.connect(str(ledger_db_path(tva_root)), read_only=True)
    try:
        prices = con.execute(
            "SELECT entry_price FROM trades WHERE tva_trade_id = 'T01'"
        ).fetchone()
    finally:
        con.close()
    assert prices is not None
    assert prices[0] == 21000.0


def test_api_ledger_summary_shape(tva_root: Path) -> None:
    _seed_ten(tva_root)
    client = TestClient(create_app(tva_root))
    body = client.get("/ledger/summary", params={"weeks": 4}).json()
    assert body["schema_version"] == "1"
    assert body["weeks"] == 4
    assert body["sessions"] == 10
    assert body["trades"] == 20
    assert body["trades_per_hour"] == 1.0
    assert body["adherence"]["rate"] == 0.5
    assert body["violations"]["total"] == 10
    assert body["stated_vs_lab"]["rate"] == 0.5
    assert [row["id"] for row in body["periods"]] == ["2026-W37", "2026-W38"]
    assert "markdown" in body
    bad = client.get("/ledger/summary", params={"weeks": 0})
    assert bad.status_code == 422
    openapi = client.get("/openapi.json").json()
    assert "/ledger/summary" in openapi["paths"]


def test_api_empty_ledger_is_zeros(tva_root: Path) -> None:
    client = TestClient(create_app(tva_root))
    body = client.get("/ledger/summary").json()
    assert body["weeks"] == 4
    assert body["sessions"] == 0
    assert body["trades"] == 0
    assert body["periods"] == []


def test_ledger_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "ledger", "add", "../outside"]) == 1
    assert main(["--root", str(tva_root), "ledger", "add", "foo/bar"]) == 1


def test_ledger_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "ledger" not in ALLOWED_RUN_STAGES


def test_ledger_omitted_from_status_until_add(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "ledger" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_ledger_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="ledger", error="ledger exploded")
    status = store.compute_status(tva_root, record.id)
    assert "ledger" not in status.stages
    assert status.error is None


def test_invalidate_and_fills_drop_ledger_row(tva_root: Path) -> None:
    record = _seed(tva_root, "2026-09-11_143000")
    add_session(record.id, root=tva_root)
    assert session_recorded(tva_root, record.id)
    store.invalidate_downstream(tva_root, record.id)
    assert not session_recorded(tva_root, record.id)
    _write_trades(tva_root, record.id)
    add_session(record.id, root=tva_root)
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "skipped"
    assert not session_recorded(tva_root, record.id)


def test_doctor_duckdb_ok(tva_root: Path) -> None:
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["duckdb"].status == "ok"


def test_invalid_week_fails(tva_root: Path) -> None:
    with pytest.raises(ValueError, match="ISO week"):
        rollup(tva_root, week="2026-13")


def test_add_without_trades_still_records_session(tva_root: Path) -> None:
    record = _session(tva_root, "2026-09-11_143000")
    result = add_session(record.id, root=tva_root)
    assert result.trades == 0
    assert session_recorded(tva_root, record.id)
    summary = build_summary(tva_root, week="2026-W37")
    assert summary.sessions == 1
    assert summary.trades == 0
    assert summary.trades_per_hour == 0.0
