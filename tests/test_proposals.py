from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import (
    confirm_proposal,
    extract_session,
    fills_session,
    proposals_session,
    transcribe_session,
)
from tradevidanalyser.proposals import (
    CSV_COLUMNS,
    NOTES_PREFIX,
    ProposalsError,
    _as_date,
    default_tag_map_path,
    load_tag_map,
    map_stated_text,
    set_proposal_status,
)
from tradevidanalyser.schema import (
    CitedSpan,
    Evidence,
    EvidenceTrade,
    EvidenceWindow,
    Insights,
    IntentProposals,
    RecordingInfo,
    SessionRecord,
    StatedCite,
    StatedFields,
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


def _write_trades(
    root: Path,
    session_id: str,
    *,
    trade_ids: list[str] | None = None,
) -> None:
    ids = trade_ids or ["T01"]
    n = len(ids)
    table = pa.table(
        {
            "tva_trade_id": ids,
            "trade_id": [f"jt-{i}" for i in range(n)],
            "direction": ["long"] * n,
            "instrument": ["MNQ"] * n,
            "entry_price": [21000.25] * n,
            "qty": [2] * n,
            "session_date": [date(2026, 9, 11)] * n,
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _write_evidence(
    root: Path,
    session_id: str,
    *,
    playbook: str = "ONH Touch",
    setup: str | None = "3c",
    trade_id: str = "T01",
    seg: str = "s1",
) -> None:
    stated = StatedFields(
        playbook=StatedCite(value=playbook, seg=seg) if playbook else None,
        setup=StatedCite(value=setup, seg=seg) if setup else None,
    )
    evidence = Evidence(
        provider="fake",
        model="none",
        prompt_version="stated-fake-v1",
        session_id=session_id,
        trades=[
            EvidenceTrade(
                tva_trade_id=trade_id,
                window=EvidenceWindow(t0=0.0, t1=60.0),
                stated=stated,
                alignment_confidence=0.9,
            )
        ],
    )
    store.write_json(store.evidence_path(root, session_id), evidence.model_dump(mode="json"))


def _write_insights(
    root: Path,
    session_id: str,
    *,
    levels: list[tuple[str, str]] | None = None,
    playbooks: list[tuple[str, str]] | None = None,
) -> None:
    insights = Insights(
        provider="fake",
        model="none",
        stated_levels=[
            CitedSpan(seg=seg, text=token, token=token) for token, seg in (levels or [])
        ],
        playbooks_mentioned=[
            CitedSpan(seg=seg, text=name, name=name) for name, seg in (playbooks or [])
        ],
    )
    store.save_insights(root, session_id, insights)


def _load_report(root: Path, session_id: str) -> IntentProposals:
    return IntentProposals.model_validate(store.read_json(store.proposals_path(root, session_id)))


def _read_csv(root: Path, session_id: str) -> list[dict[str, str]]:
    path = store.tradesviz_tags_path(root, session_id)
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def test_repo_and_package_tag_map_match() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "tag_map.yaml"
    repo = Path(__file__).resolve().parents[1] / "tag_map.yaml"
    assert package.read_text(encoding="utf-8") == repo.read_text(encoding="utf-8")


def test_vocabulary_mapping_keeps_desk_keys() -> None:
    tag_map = load_tag_map()
    assert "ONH" in tag_map.vocabulary
    assert "touch" in tag_map.vocabulary
    assert "3c" in tag_map.vocabulary
    assert "dVWAP" in tag_map.vocabulary
    assert "Scalp" not in tag_map.vocabulary
    assert "Swing" not in tag_map.vocabulary
    assert map_stated_text("ONH Touch", tag_map) == ["ONH", "touch"]
    assert map_stated_text("3c", tag_map) == ["3c"]
    assert map_stated_text("dVWAP", tag_map) == ["dVWAP"]
    assert map_stated_text("d VWAP", tag_map) == ["dVWAP"]
    assert map_stated_text("Scalp", tag_map) == []
    assert map_stated_text("Swing", tag_map) == []
    assert map_stated_text("MFR", tag_map) == []
    assert map_stated_text("ONH_retest", tag_map) == ["ONH"]
    assert map_stated_text("pdH_RTH", tag_map) == ["pdH_RTH"]
    assert map_stated_text("p30POC", tag_map) == ["p30POC"]
    invented = map_stated_text("ONH Touch Scalp invented_tag", tag_map)
    assert "ONH" in invented and "touch" in invented
    assert "Scalp" not in invented
    assert "invented_tag" not in invented
    assert all(tag in tag_map.vocabulary for tag in invented)
    # Nested context tags: ITR-C must not also emit ITR.
    assert map_stated_text("played ITR-C today", tag_map) == ["ITR-C"]
    assert "ITR" not in map_stated_text("played ITR-C today", tag_map)
    assert map_stated_text("CTR-R", tag_map) == ["CTR-R"]


def test_engine_tokens_map_to_desk_keys_never_emitted() -> None:
    tag_map = load_tag_map()
    package = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "tag_map.yaml"
    payload = yaml.safe_load(package.read_text(encoding="utf-8"))
    exact = payload["exact"]
    assert map_stated_text("pdHigh", tag_map) == ["pdH"]
    assert map_stated_text("SMA_21_5min", tag_map) == ["5m21SMA"]
    assert map_stated_text("EMA_21_5min", tag_map) == ["5m21EMA"]
    assert map_stated_text("VWAP_rolling_4h", tag_map) == ["4hVWAP"]
    assert map_stated_text("prev30mVWAP", tag_map) == ["p30VWAP"]
    assert map_stated_text("pdHigh_retest", tag_map) == ["pdH"]
    for key, spec in exact.items():
        token = spec.get("token") if isinstance(spec, dict) else None
        if not token:
            continue
        mapped = map_stated_text(str(token), tag_map)
        assert mapped == [str(key)], (token, mapped, key)
        if str(token) != str(key):
            assert str(token) not in mapped
        assert all(tag in tag_map.vocabulary for tag in mapped)


def test_proposals_map_playbook_and_levels(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id, playbook="ONH Touch", setup="3c")
    _write_insights(
        tva_root,
        record.id,
        levels=[("dVWAP", "s2")],
        playbooks=[("Scalp", "s3")],
    )
    result = proposals_session(record.id, root=tva_root)
    assert result.status == "ok"
    assert result.path == "intent_proposals.json"
    assert result.csv_path == "tradesviz_tags.csv"
    report = _load_report(tva_root, record.id)
    assert report.schema_version == "1"
    assert report.session_id == record.id
    assert len(report.proposals) == 1
    item = report.proposals[0]
    assert item.tva_trade_id == "T01"
    assert item.proposed_tags == ["ONH", "touch", "3c", "dVWAP"]
    assert item.source_segs == ["s1", "s2"]
    assert item.status == "proposed"
    assert any("Scalp" in gap for gap in report.gaps)
    assert all(tag in load_tag_map().vocabulary for tag in item.proposed_tags)
    assert store.compute_status(tva_root, record.id).stages["proposals"] == "ok"


def test_csv_columns_match_tradesviz_import(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    path = store.tradesviz_tags_path(tva_root, record.id)
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert tuple(reader.fieldnames) == CSV_COLUMNS
        rows = list(reader)
    assert rows == [
        {
            "date": "2026-09-11",
            "symbol": "MNQ",
            "side": "long",
            "price": "21000.25",
            "quantity": "2",
            "tags": "ONH,touch,3c",
            "notes": f"{NOTES_PREFIX} T01",
        }
    ]
    assert rows[0]["notes"].startswith("[TVA proposed]")


def test_confirm_toggles_proposed_and_confirmed(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    first = confirm_proposal("T01", root=tva_root)
    assert first.status == "confirmed"
    assert _load_report(tva_root, record.id).proposals[0].status == "confirmed"
    second = confirm_proposal(f"{record.id}:T01", root=tva_root)
    assert second.status == "proposed"
    assert _load_report(tva_root, record.id).proposals[0].status == "proposed"


def test_rejected_omitted_from_csv_and_toggles_back(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    set_proposal_status("T01", root=tva_root, status="rejected")
    assert _load_report(tva_root, record.id).proposals[0].status == "rejected"
    assert _read_csv(tva_root, record.id) == []
    toggled = confirm_proposal("T01", root=tva_root)
    assert toggled.status == "proposed"
    assert _read_csv(tva_root, record.id)[0]["notes"] == f"{NOTES_PREFIX} T01"


def test_rerun_preserves_existing_status(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    confirm_proposal("T01", root=tva_root)
    proposals_session(record.id, root=tva_root)
    assert _load_report(tva_root, record.id).proposals[0].status == "confirmed"


def test_ambiguous_trade_id_requires_session_prefix(tva_root: Path) -> None:
    first = _session(tva_root, "2026-09-11_143000")
    second = _session(tva_root, "2026-09-12_143000")
    for record in (first, second):
        _write_trades(tva_root, record.id)
        _write_evidence(tva_root, record.id)
        proposals_session(record.id, root=tva_root)
    with pytest.raises(ProposalsError, match="ambiguous"):
        confirm_proposal("T01", root=tva_root)
    result = confirm_proposal(f"{second.id}:T01", root=tva_root)
    assert result.session_id == second.id
    assert result.status == "confirmed"


def test_cli_proposals_and_confirm(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    assert main(["--root", str(tva_root), "proposals", record.id]) == 0
    out = capsys.readouterr().out
    assert "intent_proposals.json" in out
    assert "tradesviz_tags.csv" in out
    assert main(["--root", str(tva_root), "proposals", "confirm", "T01"]) == 0
    assert _load_report(tva_root, record.id).proposals[0].status == "confirmed"
    assert main(["--root", str(tva_root), "proposals", "confirm"]) == 1
    assert "confirm requires" in capsys.readouterr().out


def test_proposals_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "proposals", "../outside"]) == 1
    assert main(["--root", str(tva_root), "proposals", "foo/bar"]) == 1
    assert main(["--root", str(tva_root), "proposals", "confirm", "../outside"]) == 1


def test_proposals_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "proposals" not in ALLOWED_RUN_STAGES


def test_proposals_omitted_from_status_until_run(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "proposals" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_proposals_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="proposals", error="proposals exploded")
    status = store.compute_status(tva_root, record.id)
    assert "proposals" not in status.stages
    assert status.error is None


def test_half_stage_json_alone_is_not_ok(tva_root: Path) -> None:
    record = _session(tva_root)
    store.write_json(
        store.proposals_path(tva_root, record.id),
        {"schema_version": "1", "session_id": record.id, "proposals": []},
    )
    assert "proposals" not in store.compute_status(tva_root, record.id).stages


def test_invalidate_downstream_drops_proposals(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    assert store.proposals_path(tva_root, record.id).is_file()
    assert store.tradesviz_tags_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.proposals_path(tva_root, record.id).is_file()
    assert not store.tradesviz_tags_path(tva_root, record.id).is_file()
    assert "proposals" not in store.compute_status(tva_root, record.id).stages


def test_fills_skip_drops_stale_proposals(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    csv_path = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv_path, venue="amp")
    assert result.status == "skipped"
    assert not store.proposals_path(tva_root, record.id).is_file()
    assert not store.tradesviz_tags_path(tva_root, record.id).is_file()


def test_fills_rewrite_drops_stale_proposals(tva_root: Path) -> None:
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
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    assert store.proposals_path(tva_root, record.id).is_file()
    csv_path = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv_path, venue="amp")
    assert result.status == "ok"
    assert not store.proposals_path(tva_root, record.id).is_file()
    assert not store.tradesviz_tags_path(tva_root, record.id).is_file()
    assert "proposals" not in store.compute_status(tva_root, record.id).stages


def test_session_levels_attach_only_when_spoken_or_single_trade(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, trade_ids=["T01", "T02"])
    evidence = Evidence(
        provider="fake",
        model="none",
        prompt_version="stated-fake-v1",
        session_id=record.id,
        trades=[
            EvidenceTrade(
                tva_trade_id="T01",
                window=EvidenceWindow(t0=0.0, t1=60.0),
                stated=StatedFields(playbook=StatedCite(value="ONH", seg="s1")),
                alignment_confidence=0.9,
            ),
            EvidenceTrade(
                tva_trade_id="T02",
                window=EvidenceWindow(t0=80.0, t1=120.0),
                stated=StatedFields(playbook=StatedCite(value="3c", seg="s2")),
                alignment_confidence=0.9,
            ),
        ],
    )
    store.write_json(store.evidence_path(tva_root, record.id), evidence.model_dump(mode="json"))
    _write_insights(tva_root, record.id, levels=[("dVWAP", "s3"), ("ONH", "s4")])
    proposals_session(record.id, root=tva_root)
    by_id = {item.tva_trade_id: item for item in _load_report(tva_root, record.id).proposals}
    assert by_id["T01"].proposed_tags == ["ONH"]
    assert by_id["T02"].proposed_tags == ["3c"]
    assert "dVWAP" not in by_id["T01"].proposed_tags
    assert "dVWAP" not in by_id["T02"].proposed_tags


def test_session_onh_does_not_attach_to_ponh_trade(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, trade_ids=["T01", "T02"])
    evidence = Evidence(
        provider="fake",
        model="none",
        prompt_version="stated-fake-v1",
        session_id=record.id,
        trades=[
            EvidenceTrade(
                tva_trade_id="T01",
                window=EvidenceWindow(t0=0.0, t1=60.0),
                stated=StatedFields(playbook=StatedCite(value="pONH", seg="s1")),
                alignment_confidence=0.9,
            ),
            EvidenceTrade(
                tva_trade_id="T02",
                window=EvidenceWindow(t0=80.0, t1=120.0),
                stated=StatedFields(playbook=StatedCite(value="ONH", seg="s2")),
                alignment_confidence=0.9,
            ),
        ],
    )
    store.write_json(store.evidence_path(tva_root, record.id), evidence.model_dump(mode="json"))
    _write_insights(tva_root, record.id, levels=[("ONH", "s3")])
    proposals_session(record.id, root=tva_root)
    by_id = {item.tva_trade_id: item for item in _load_report(tva_root, record.id).proposals}
    assert by_id["T01"].proposed_tags == ["pONH"]
    assert by_id["T02"].proposed_tags == ["ONH"]
    assert "ONH" not in by_id["T01"].proposed_tags


def test_csv_date_is_calendar_day_not_isoformat_datetime(tva_root: Path) -> None:
    record = _session(tva_root)
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "trade_id": ["jt-0"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.25],
            "qty": [2],
            "session_date": [datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc)],
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    rows = _read_csv(tva_root, record.id)
    assert rows[0]["date"] == "2026-09-11"
    assert "T" not in rows[0]["date"]
    assert "+" not in rows[0]["date"]
    assert _as_date(datetime(2026, 9, 11, 22, 0, tzinfo=timezone.utc), "2026-09-12") == "2026-09-11"
    assert _as_date(datetime(2026, 9, 11, 0, 0), "2026-09-12") == "2026-09-11"
    assert _as_date(date(2026, 9, 11), "2026-09-12") == "2026-09-11"


def test_insights_only_does_not_invent_trade(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_insights(tva_root, record.id, levels=[("dVWAP", "s1")], playbooks=[("ONH Touch", "s2")])
    result = proposals_session(record.id, root=tva_root)
    assert result.status == "ok"
    report = _load_report(tva_root, record.id)
    assert report.proposals == []
    assert any("no trades" in gap for gap in report.gaps)
    assert _read_csv(tva_root, record.id) == []


def test_confirm_skips_corrupt_sibling_session(tva_root: Path) -> None:
    first = _session(tva_root, "2026-09-11_143000")
    second = _session(tva_root, "2026-09-12_143000")
    _write_trades(tva_root, first.id)
    _write_evidence(tva_root, first.id)
    proposals_session(first.id, root=tva_root)
    store.write_json(store.proposals_path(tva_root, second.id), {"not": "a proposals file"})
    result = confirm_proposal("T01", root=tva_root)
    assert result.session_id == first.id
    assert result.status == "confirmed"


def test_csv_keeps_zero_quantity(tva_root: Path) -> None:
    record = _session(tva_root)
    table = pa.table(
        {
            "tva_trade_id": ["T01"],
            "trade_id": ["jt-0"],
            "direction": ["long"],
            "instrument": ["MNQ"],
            "entry_price": [21000.25],
            "qty": [0],
            "session_date": [date(2026, 9, 11)],
        }
    )
    path = store.trades_path(tva_root, record.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)
    _write_evidence(tva_root, record.id)
    proposals_session(record.id, root=tva_root)
    assert _read_csv(tva_root, record.id)[0]["quantity"] == "0"


def test_doctor_tag_map_ok(tva_root: Path) -> None:
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["tag_map"].status == "ok"
    assert Path(ids["tag_map"].detail).is_file()
    assert default_tag_map_path().is_file()
