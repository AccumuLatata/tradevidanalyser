from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.context import FakeNotionClient, context_session
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ocr import OcrRow, write_ocr_parquet
from tradevidanalyser.pipeline import (
    align_session,
    evidence_session,
    extract_session,
    fills_session,
    ocr_session,
    rules_session,
    transcribe_session,
)
from tradevidanalyser.report import (
    ENV_REPORT,
    ENV_XAI_KEY,
    SECTION_HEADINGS,
    SECTION_SPECS,
    FakeReportProvider,
    ProseSpan,
    ReportProse,
    _cell_text,
    build_debrief,
    collect_allowed_runs,
    digit_audit,
    gather_facts,
    render_debrief,
    render_markdown,
    report_session,
)
from tradevidanalyser.schema import (
    BriefContext,
    LabContext,
    LabTradeContext,
    RecordingInfo,
    SessionContext,
    SessionRecord,
)
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app


def _session(root: Path, session_id: str = "2026-09-11_143000") -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_trades(root: Path, session_id: str) -> None:
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "trade_id": ["jt-1"],
            "entry_fill_id": ["fill-a"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [20990.0],
            "net_pnl_currency": [-10.0],
            "gross_pnl_currency": [None],
            "status": ["closed"],
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _write_context(root: Path, session_id: str) -> None:
    store.write_json(
        store.context_path(root, session_id),
        SessionContext(
            session_id=session_id,
            brief=BriefContext(
                macro_url="https://notion.so/macro",
                ny_url="https://notion.so/ny",
                bias_nq="long ONH",
                bias_es="neutral",
                conviction="high",
                kill_levels="18480",
                quoted=True,
            ),
            lab=LabContext(
                per_trade={
                    "T01": LabTradeContext(
                        nearest_level_token="pdPOC",
                        level_context="at_level",
                        tag_alignment="all_aligned",
                    )
                }
            ),
            gaps=["lab-dir not set"],
        ).model_dump(mode="json"),
    )


def _write_rules(root: Path, session_id: str) -> None:
    store.write_json(
        store.rules_path(root, session_id),
        {
            "schema_version": "1",
            "session_id": session_id,
            "rules": [
                {"rule": "R-DLL", "status": "unverifiable", "evidence": {}, "reason": None},
                {"rule": "R-MAX10", "status": "pass", "evidence": {}, "reason": None},
                {"rule": "R-PLAYBOOK", "status": "pass", "evidence": {"segs": ["seg_001"]}, "reason": None},
            ],
        },
    )


def test_section_order_and_digit_audit(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    _write_rules(tva_root, record.id)
    result = report_session(record.id, root=tva_root)
    assert result.status == "ok"
    markdown = store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8")
    positions = [markdown.index(heading) for heading in SECTION_HEADINGS]
    assert positions == sorted(positions)
    allowed = collect_allowed_runs(tva_root, record.id)
    assert not digit_audit(markdown, allowed)
    payload = store.read_json(store.debrief_json_path(tva_root, record.id))
    assert [row["id"] for row in payload["sections"]] == [sid for sid, _title, _kind in SECTION_SPECS]
    assert payload["provider"] == "fake"
    assert "T01" in markdown
    assert "21000" in markdown
    assert "Daily loss limit" in markdown
    assert "R-3L30" not in markdown
    assert "Reviewed the tape against T01" in markdown
    assert "pdPOC" in markdown


def test_cli_report_is_byte_identical(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    assert "report" not in store.compute_status(tva_root, record.id).stages
    assert main(["--root", str(tva_root), "report", record.id]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    first_md = store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8")
    first_json = store.debrief_json_path(tva_root, record.id).read_text(encoding="utf-8")
    assert main(["--root", str(tva_root), "report", record.id]) == 0
    assert store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8") == first_md
    assert store.debrief_json_path(tva_root, record.id).read_text(encoding="utf-8") == first_json


def test_clip_filename_digits_stay_out_of_markdown(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    store.write_json(
        store.evidence_path(tva_root, record.id),
        {"trades": [{"tva_trade_id": "T01", "clip": "clips/0120.000.mp4"}]},
    )
    report_session(record.id, root=tva_root)
    markdown = store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8")
    assert "0120" not in markdown
    assert "| clip |" in markdown or "| clip" in markdown
    payload = store.read_json(store.debrief_json_path(tva_root, record.id))
    trades = next(row for row in payload["sections"] if row["id"] == "trades")
    assert "clips/0120.000.mp4" in trades["cites"]
    assert not digit_audit(markdown, collect_allowed_runs(tva_root, record.id))


def test_uncited_and_invented_digits_are_dropped(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    facts = gather_facts(record, root=tva_root)
    prose = ReportProse(
        day=ProseSpan(text="Made up PnL 99999.", cites=["T01"]),
        brief_vs_behaviour=[ProseSpan(text="No cite here.", cites=[])],
        observations=[ProseSpan(text="Quoted from nowhere.", cites=["seg_999"])],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    markdown = render_markdown(report)
    assert "99999" not in markdown
    assert "No cite here." not in markdown
    assert "Quoted from nowhere." not in markdown
    assert any("digit not in trades" in gap for gap in report.gaps)
    assert any("uncited" in gap for gap in report.gaps)
    assert not digit_audit(markdown, facts.allowed_runs)


def test_grok_uses_mocked_transport(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    monkeypatch.setenv(ENV_XAI_KEY, "secret")
    monkeypatch.setenv(ENV_REPORT, "grok")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        body = {
            "day": {"text": "Reviewed the tape against T01.", "cites": ["T01"]},
            "brief_vs_behaviour": [
                {"text": "Brief NQ bias: long ONH.", "cites": ["brief"]},
            ],
            "observations": [{"text": "Notes recorded for T01.", "cites": ["T01"]}],
            "learnings": [
                {"text": "Name the playbook before entry (T01).", "cites": ["T01"]},
                {"text": "State stop and target before entry (T01).", "cites": ["T01"]},
                {"text": "Cool down after tilt language (T01).", "cites": ["T01"]},
            ],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(body)}}]},
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = build_debrief(record, root=tva_root, provider_name="grok", client=client)
    assert report.provider == "grok"
    assert "Reviewed the tape against T01." in render_markdown(report)


def test_default_fake_never_calls_grok(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    monkeypatch.delenv(ENV_REPORT, raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("live Grok must not be called in CI")

    monkeypatch.setattr("httpx.Client.post", boom)
    result = report_session(record.id, root=tva_root)
    assert result.provider == "fake"


def test_grok_without_key_fails(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _session(tva_root)
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    with pytest.raises(ValueError, match="XAI_API_KEY"):
        report_session(record.id, root=tva_root, provider_name="grok")


def test_report_omitted_from_status_until_run(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "report" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_report_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="report", error="report exploded")
    status = store.compute_status(tva_root, record.id)
    assert "report" not in status.stages
    assert status.error is None


def test_report_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "report", "../outside"]) == 1
    assert main(["--root", str(tva_root), "report", "foo/bar"]) == 1


def test_report_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "report" not in ALLOWED_RUN_STAGES


def test_invalidate_downstream_drops_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    assert store.debrief_md_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.debrief_md_path(tva_root, record.id).is_file()
    assert not store.debrief_json_path(tva_root, record.id).is_file()


def test_fills_skip_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "skipped"
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_doctor_report_provider_fake_ok(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_REPORT, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["report_provider"].status == "ok"


def test_doctor_report_grok_without_key_fails(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_REPORT, "grok")
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["report_provider"].status == "fail"


@pytest.mark.golden
def test_golden_debrief_digit_audit() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    paths = list(golden.rglob("debrief.md"))
    if not paths:
        pytest.skip("no golden debrief.md")
    for path in paths:
        session_dir = path.parent
        root = session_dir.parent.parent
        session_id = session_dir.name
        allowed = collect_allowed_runs(root, session_id)
        assert not digit_audit(path.read_text(encoding="utf-8"), allowed)


def test_zero_net_pnl_is_not_replaced_by_gross(tva_root: Path) -> None:
    record = _session(tva_root)
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [21000.0],
            "net_pnl_currency": [0.0],
            "gross_pnl_currency": [99.0],
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    report_session(record.id, root=tva_root)
    markdown = store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8")
    assert "| 0 |" in markdown
    assert "99" not in markdown


def test_schema_version_digit_is_not_allowlisted(tva_root: Path) -> None:
    record = _session(tva_root)
    table = pa.table(
        {
            "tva_trade_id": ["T02"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [20990.0],
            "net_pnl_currency": [-10.0],
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    _write_context(tva_root, record.id)
    facts = gather_facts(record, root=tva_root)
    assert "1" not in facts.allowed_runs
    prose = ReportProse(
        day=ProseSpan(text="Took 1 extra scalp.", cites=["T02"]),
        brief_vs_behaviour=[],
        observations=[],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    markdown = render_markdown(report)
    assert "Took 1 extra scalp." not in markdown
    assert any("digit not in trades" in gap for gap in report.gaps)


def test_ocr_confidence_and_time_digits_are_not_allowlisted(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    write_ocr_parquet(
        store.ocr_path(tva_root, record.id),
        [OcrRow(t=777.0, roi="clock", text="flat", confidence=0.88, parsed=None)],
    )
    facts = gather_facts(record, root=tva_root)
    assert "777" not in facts.allowed_runs
    assert "88" not in facts.allowed_runs
    prose = ReportProse(
        day=ProseSpan(text="Clock read 777 with confidence 88.", cites=["T01"]),
        brief_vs_behaviour=[],
        observations=[],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    assert "Clock read 777" not in render_markdown(report)


def test_unknown_cite_drops_span_even_with_valid_sibling(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    facts = gather_facts(record, root=tva_root)
    prose = ReportProse(
        day=ProseSpan(text="Reviewed the tape against T01.", cites=["T01", "T99"]),
        brief_vs_behaviour=[],
        observations=[],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    assert "Reviewed the tape against T01." not in render_markdown(report)
    assert any("unknown cite" in gap for gap in report.gaps)


def test_invented_id_in_prose_is_dropped(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    facts = gather_facts(record, root=tva_root)
    prose = ReportProse(
        day=ProseSpan(text="See T99 for the chase.", cites=["T01"]),
        brief_vs_behaviour=[],
        observations=[],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    assert "T99" not in render_markdown(report)
    assert any("invented id" in gap for gap in report.gaps)


def test_brief_cite_rejected_when_brief_missing(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    facts = gather_facts(record, root=tva_root)
    assert "brief" not in facts.allowed_ids
    prose = ReportProse(
        day=ProseSpan(text="Reviewed available files.", cites=["brief"]),
        brief_vs_behaviour=[],
        observations=[],
        learnings=[],
    )
    report = render_debrief(record, facts, prose, root=tva_root, provider=FakeReportProvider())
    assert "Reviewed available files." not in render_markdown(report)
    assert any("unknown cite" in gap for gap in report.gaps)


def test_missing_trade_id_is_not_invented(tva_root: Path) -> None:
    record = _session(tva_root)
    table = pa.table(
        {
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [20990.0],
            "net_pnl_currency": [-10.0],
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    result = report_session(record.id, root=tva_root)
    assert result.status == "ok"
    markdown = store.debrief_md_path(tva_root, record.id).read_text(encoding="utf-8")
    assert "T01" not in markdown
    assert "21000" in markdown


def test_vienna_fill_clock_digits_are_allowed(tva_root: Path) -> None:
    record = _session(tva_root)
    utc = datetime(2026, 9, 11, 12, 30, tzinfo=timezone.utc)
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [20990.0],
            "net_pnl_currency": [-10.0],
            "entry_timestamp": pa.array([utc], type=pa.timestamp("us", tz="UTC")),
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    allowed = collect_allowed_runs(tva_root, record.id)
    assert "12" in allowed
    assert "14" in allowed
    assert _cell_text(utc) == "2026-09-11T14:30:00+02:00"


def test_half_written_debrief_is_not_ok(tva_root: Path) -> None:
    record = _session(tva_root)
    store.debrief_md_path(tva_root, record.id).write_text("# Debrief\n", encoding="utf-8")
    status = store.compute_status(tva_root, record.id)
    assert "report" not in status.stages


def test_collect_allowed_runs_refuses_path_escape(tva_root: Path) -> None:
    with pytest.raises(ValueError, match="unsafe session id"):
        collect_allowed_runs(tva_root, "../outside")


def test_grok_accepts_list_message_content(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_context(tva_root, record.id)
    monkeypatch.setenv(ENV_XAI_KEY, "secret")

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "day": {"text": "Reviewed the tape against T01.", "cites": ["T01"]},
            "brief_vs_behaviour": [],
            "observations": [],
            "learnings": [
                {"text": "Name the playbook before entry (T01).", "cites": ["T01"]},
                {"text": "State stop and target before entry (T01).", "cites": ["T01"]},
                {"text": "Cool down after tilt language (T01).", "cites": ["T01"]},
            ],
        }
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": json.dumps(body)},
                            ]
                        }
                    }
                ]
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    report = build_debrief(record, root=tva_root, provider_name="grok", client=client)
    assert "Reviewed the tape against T01." in render_markdown(report)


def test_context_rewrite_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    assert store.debrief_md_path(tva_root, record.id).is_file()
    context_session(record.id, root=tva_root, notion=FakeNotionClient())
    assert not store.debrief_md_path(tva_root, record.id).is_file()
    assert not store.debrief_json_path(tva_root, record.id).is_file()
    assert "report" not in store.compute_status(tva_root, record.id).stages


def test_rules_rewrite_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    assert store.debrief_md_path(tva_root, record.id).is_file()
    rules_session(record.id, root=tva_root)
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_rules_skip_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    store.trades_path(tva_root, record.id).unlink()
    result = rules_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_evidence_rewrite_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    utc = datetime(2026, 9, 11, 12, 35, tzinfo=timezone.utc)
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.0],
            "exit_price": [20990.0],
            "net_pnl_currency": [-10.0],
            "entry_timestamp": pa.array([utc], type=pa.timestamp("us", tz="UTC")),
            "exit_timestamp": pa.array([utc], type=pa.timestamp("us", tz="UTC")),
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    report_session(record.id, root=tva_root)
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "ok"
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_align_rewrite_drops_stale_debrief(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    align_session(record.id, root=tva_root)
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_ocr_rewrite_drops_stale_debrief(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    monkeypatch.setattr("tradevidanalyser.pipeline.ocr_frames", lambda *_args, **_kwargs: [])
    ocr_session(record.id, root=tva_root)
    assert not store.debrief_md_path(tva_root, record.id).is_file()


def test_fills_rewrite_drops_stale_debrief(tva_root: Path) -> None:
    record = SessionRecord(
        id="2026-05-14_160000",
        recording=RecordingInfo(
            path="recordings/2026-05-14 16-00-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-05-14T16:00:00+02:00",
            duration_s=3600.0,
            filename="2026-05-14 16-00-00.mp4",
        ),
    )
    store.save_session(tva_root, record)
    _write_trades(tva_root, record.id)
    report_session(record.id, root=tva_root)
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "ok"
    assert not store.debrief_md_path(tva_root, record.id).is_file()
