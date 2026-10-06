from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.coach import coach_session
from tradevidanalyser.day_manifest import (
    DAY_STALE_ERROR,
    DayStale,
    MANIFEST_OFF_ERROR,
    PAUSE_KEY_ERROR,
    TZ_ASSUMPTION_CSV,
    build_day,
    current_pointer_path,
    day_dir,
    day_lock_path,
    day_state,
    delete_day_manifest,
    enable_day_manifest,
    enabled_from_path,
    load_current_day_json,
    read_enabled_from,
    require_fresh,
    require_fresh_store,
)
from tradevidanalyser.ledger import ledger_summary, window_session_ids, window_trade_facts
from tradevidanalyser.flags import ENV_DAY_MANIFEST, ENV_PAUSE_GUARD
from tradevidanalyser.fills_mirror import FILL_RECORD_COLUMNS
from tradevidanalyser.ingest import SPLIT_GAP_S, ingest
from tradevidanalyser.pipeline import fills_session, publish_session as pipeline_publish
from tradevidanalyser.schema import (
    DebriefReport,
    DebriefSection,
    Evidence,
    RecordingInfo,
    RecordingPart,
    SessionRecord,
)
from tradevidanalyser.serve import create_app

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_SYNTHETIC = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
L0_SESSION = "2026-05-14_160300"
VIENNA = timezone(timedelta(hours=2))
CET = timezone(timedelta(hours=1))
CEST = timezone(timedelta(hours=2))

CSV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)


def _session(
    root: Path,
    session_id: str,
    *,
    start: datetime,
    duration_s: float = 3600.0,
    sha256: str | None = None,
    filename: str | None = None,
    parts: list[RecordingPart] | None = None,
) -> SessionRecord:
    name = filename or f"{session_id[:10]} {session_id[11:13]}-{session_id[13:15]}-{session_id[15:17]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{name}",
            sha256=sha256 or ("a" * 64),
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename=name,
            parts=parts or [],
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_csv(path: Path, rows: list[str] | None = None) -> Path:
    body = CSV_HEADER + "\n"
    if rows:
        body += "\n".join(rows) + "\n"
    path.write_text(body, encoding="utf-8")
    return path


def _csv_row(ts: str, *, spread: str = "g1") -> str:
    return (
        f"{ts},MNQM26,buy,USD,MNQ,future,21000.0,1.0,"
        f"0,0,N/A,N/A,,,{spread}"
    )


def _write_evidence(root: Path, session_id: str) -> Path:
    path = store.evidence_path(root, session_id)
    store.write_json(
        path,
        Evidence(
            provider="fake",
            model="none",
            prompt_version="test",
            session_id=session_id,
            trades=[],
        ).model_dump(mode="json"),
    )
    return path


def _write_debrief(root: Path, session_id: str) -> Path:
    path = store.debrief_json_path(root, session_id)
    store.write_json(path, {"schema_version": "1", "session_id": session_id, "learnings": []})
    store.debrief_md_path(root, session_id).write_text("# debrief\n", encoding="utf-8")
    return path


def _write_publish_debrief(root: Path, session_id: str) -> None:
    report = DebriefReport(
        session_id=session_id,
        provider="fake",
        model="none",
        prompt_version="debrief-fake-v1",
        sections=[
            DebriefSection(
                id="day",
                title="Day",
                kind="prose",
                body="Reviewed the tape against T01. Extra clause stays out.",
            ),
            DebriefSection(
                id="learnings",
                title="Learnings",
                kind="prose",
                body="Name the playbook before entry.\nState stop and target.\nCool down after tilt.",
            ),
        ],
    )
    store.write_json(store.debrief_json_path(root, session_id), report.model_dump(mode="json"))
    store.debrief_md_path(root, session_id).write_text("# Debrief\n", encoding="utf-8")


def _write_rules(root: Path, session_id: str) -> Path:
    path = store.rules_path(root, session_id)
    store.write_json(path, {"schema_version": "1", "session_id": session_id, "rules": []})
    return path


def _on(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    for name in names:
        monkeypatch.setenv(name, "1")


def test_flag_off_day_state_is_legacy(tva_root: Path) -> None:
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    day = date(2026, 9, 14)
    state = day_state(tva_root, day)
    assert state.kind == "legacy"
    require_fresh(tva_root, day)
    assert not (tva_root / "days").exists()


def test_missing_enabled_from_is_legacy_unbuilt_and_reader_does_not_write(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    day = date(2026, 9, 14)
    state = day_state(tva_root, day)
    assert state.kind == "legacy_unbuilt"
    require_fresh(tva_root, day)
    require_fresh_store(tva_root)
    store.compute_status(tva_root, "2026-09-14_093000")
    assert read_enabled_from(tva_root) is None
    assert not enabled_from_path(tva_root).exists()


def test_day_before_enabled_from_without_current_is_legacy_unbuilt(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    enable_day_manifest(tva_root, date(2026, 9, 20))
    state = day_state(tva_root, date(2026, 9, 14))
    assert state.kind == "legacy_unbuilt"
    require_fresh(tva_root, date(2026, 9, 14))


def test_two_days_stay_separate(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    _session(
        tva_root,
        "2026-09-15_093000",
        start=datetime(2026, 9, 15, 9, 30, tzinfo=VIENNA),
        sha256="c" * 64,
    )
    _session(
        tva_root,
        "2026-09-15_140000",
        start=datetime(2026, 9, 15, 14, 0, tzinfo=VIENNA),
        sha256="d" * 64,
    )
    csv = _write_csv(tmp_path / "exec.csv")
    first = build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    second = build_day(tva_root, date(2026, 9, 15), executions=csv, venue="amp")
    assert first.written and second.written
    a = load_current_day_json(tva_root, date(2026, 9, 14))
    b = load_current_day_json(tva_root, date(2026, 9, 15))
    assert a is not None and b is not None
    assert a["date"] == "2026-09-14"
    assert b["date"] == "2026-09-15"
    assert {row["session_id"] for row in a["clips"]} == {
        "2026-09-14_093000",
        "2026-09-14_140000",
    }
    assert {row["session_id"] for row in b["clips"]} == {
        "2026-09-15_093000",
        "2026-09-15_140000",
    }
    assert a["tz_assumption"] == TZ_ASSUMPTION_CSV
    assert b["tz_assumption"] == TZ_ASSUMPTION_CSV


def test_m3_one_session_writes_no_days(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0)
    record = ingest(tmp_path / "2026-09-11 14-30-02.mp4", root=tva_root)
    assert record.id == "2026-09-11_143000"
    assert store.list_session_ids(tva_root) == [record.id]
    assert len(record.recording.parts) == 2
    csv = _write_csv(tmp_path / "exec.csv")
    result = build_day(tva_root, date(2026, 9, 11), executions=csv, venue="amp")
    assert result.written is False
    assert result.status == "legacy"
    assert not day_dir(tva_root, date(2026, 9, 11)).exists()
    assert read_enabled_from(tva_root) == date(2026, 9, 11)
    assert day_state(tva_root, date(2026, 9, 11)).kind == "legacy"


def test_m4_two_sessions_write_day_json(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    assert SPLIT_GAP_S == 5.0
    write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    write_video(tmp_path / "2026-09-11 14-30-08.mp4", seconds=2.0)
    first = ingest(tmp_path / "2026-09-11 14-30-00.mp4", root=tva_root)
    second = ingest(tmp_path / "2026-09-11 14-30-08.mp4", root=tva_root)
    assert first.id != second.id
    assert sorted(store.list_session_ids(tva_root)) == sorted([first.id, second.id])
    csv = _write_csv(
        tmp_path / "exec.csv",
        [_csv_row("2026-09-11T14:40:00+0000")],
    )
    result = build_day(tva_root, date(2026, 9, 11), executions=csv, venue="amp")
    assert result.written is True
    payload = load_current_day_json(tva_root, date(2026, 9, 11))
    assert payload is not None
    assert payload["schema_version"] == "1"
    assert payload["tz_assumption"] == TZ_ASSUMPTION_CSV
    assert [row["session_id"] for row in payload["clips"]] == [first.id, second.id]
    assert all(row["cascade"] == "unchanged" for row in payload["clips"])
    assert all(row["pause_check"] == "guard_off" for row in payload["clips"])
    assert payload["outside"] == []
    assert payload["trades_outside_clips"] == 0
    assert day_state(tva_root, date(2026, 9, 11)).kind in {"current", "current_incomplete"}


def test_m14_identical_builds_and_stale_without_rebuild(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    starts = [
        ("2026-09-14_090000", datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA), "a" * 64),
        ("2026-09-14_120000", datetime(2026, 9, 14, 12, 0, tzinfo=VIENNA), "b" * 64),
        ("2026-09-14_150000", datetime(2026, 9, 14, 15, 0, tzinfo=VIENNA), "c" * 64),
    ]
    for session_id, start, sha in starts:
        _session(tva_root, session_id, start=start, sha256=sha)
        _write_evidence(tva_root, session_id)
        _write_debrief(tva_root, session_id)
    csv = _write_csv(
        tmp_path / "exec.csv",
        [
            _csv_row("2026-09-13T18:00:00+0000"),
            _csv_row("2026-09-14T10:00:00+0000", spread="g2"),
            _csv_row("2026-09-15T08:00:00+0000", spread="g3"),
        ],
    )
    day = date(2026, 9, 14)
    first = build_day(tva_root, day, executions=csv, venue="amp")
    assert first.written
    payload_a = load_current_day_json(tva_root, day)
    assert payload_a is not None
    delete_day_manifest(tva_root, day)
    enable_day_manifest(tva_root, day)
    assert build_day(tva_root, day, executions=csv, venue="amp").written
    payload_b = load_current_day_json(tva_root, day)
    assert payload_b is not None
    assert payload_a == payload_b
    extra = _session(
        tva_root,
        "2026-09-14_170000",
        start=datetime(2026, 9, 14, 17, 0, tzinfo=VIENNA),
        sha256="d" * 64,
    )
    state = day_state(tva_root, day)
    assert state.kind == "stale"
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        require_fresh(tva_root, day)
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        fills_session(extra.id, root=tva_root, executions=csv, venue="amp")


def test_dst_sort_uses_utc_instant(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    late_as_text = _session(
        tva_root,
        "2026-10-25_021000",
        start=datetime(2026, 10, 25, 2, 10, tzinfo=CET),
        sha256="e" * 64,
        filename="2026-10-25 02-10-00.mp4",
    )
    early_as_utc = _session(
        tva_root,
        "2026-10-25_023000",
        start=datetime(2026, 10, 25, 2, 30, tzinfo=CEST),
        sha256="f" * 64,
        filename="2026-10-25 02-30-00.mp4",
    )
    assert late_as_text.recording.start_wallclock_vienna < early_as_utc.recording.start_wallclock_vienna
    csv = _write_csv(tmp_path / "exec.csv")
    build_day(tva_root, date(2026, 10, 25), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 10, 25))
    assert payload is not None
    assert [row["session_id"] for row in payload["clips"]] == [
        early_as_utc.id,
        late_as_text.id,
    ]


def test_first_manifest_build_leaves_evidence(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    a = _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    b = _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    ev_a = _write_evidence(tva_root, a.id)
    ev_b = _write_evidence(tva_root, b.id)
    rules_a = _write_rules(tva_root, a.id)
    before_a = ev_a.read_text(encoding="utf-8")
    before_b = ev_b.read_text(encoding="utf-8")
    before_rules = rules_a.read_text(encoding="utf-8")
    csv = _write_csv(tmp_path / "exec.csv")
    result = build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert result.written
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert {row["cascade"] for row in payload["clips"]} == {"unchanged"}
    assert ev_a.read_text(encoding="utf-8") == before_a
    assert ev_b.read_text(encoding="utf-8") == before_b
    assert rules_a.read_text(encoding="utf-8") == before_rules


def test_second_build_new_clip_leaves_others_unchanged(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    a = _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    b = _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    _write_evidence(tva_root, a.id)
    _write_evidence(tva_root, b.id)
    csv = _write_csv(tmp_path / "exec.csv")
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    before = store.evidence_path(tva_root, a.id).read_text(encoding="utf-8")
    _session(
        tva_root,
        "2026-09-14_170000",
        start=datetime(2026, 9, 14, 17, 0, tzinfo=VIENNA),
        sha256="c" * 64,
    )
    result = build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert result.written
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    by_id = {row["session_id"]: row for row in payload["clips"]}
    assert by_id[a.id]["cascade"] == "unchanged"
    assert by_id[b.id]["cascade"] == "unchanged"
    assert by_id["2026-09-14_170000"]["cascade"] == "unchanged"
    assert store.evidence_path(tva_root, a.id).read_text(encoding="utf-8") == before


def test_l0_with_manifest_writes_no_days(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    src = L0_DIR / "l0-ocr"
    dest = config.session_dir(tva_root, L0_SESSION)
    dest.mkdir(parents=True)
    before: dict[str, str] = {}
    for name in ("session.json", "evidence.json", "rules.json", "debrief.json"):
        shutil.copy2(src / name, dest / name)
        before[name] = (dest / name).read_text(encoding="utf-8")
    shutil.copy2(src / "fills.parquet", dest / "fills.parquet")
    shutil.copy2(src / "trades.parquet", dest / "trades.parquet")
    assert day_state(tva_root, date(2026, 5, 14)).kind == "legacy_unbuilt"
    assert not (tva_root / "days").exists()
    result = build_day(
        tva_root,
        date(2026, 5, 14),
        executions=L0_SYNTHETIC,
        venue="amp",
    )
    assert result.written is False
    assert not day_dir(tva_root, date(2026, 5, 14)).exists()
    assert not list((tva_root / "days").glob("2026-05-14/**/*"))
    for name, text in before.items():
        assert (dest / name).read_text(encoding="utf-8") == text


def test_cli_day_build_enable_delete_state(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    csv = _write_csv(tmp_path / "exec.csv")
    audit = tva_root / "days" / "audit.json"
    audit.parent.mkdir(parents=True)
    audit.write_text("{}", encoding="utf-8")
    assert main(["--root", str(tva_root), "day", "enable", "2026-09-14"]) == 0
    capsys.readouterr()
    assert main(["--root", str(tva_root), "day", "state", "2026-09-14"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["day_state"] == "missing"
    assert main(
        [
            "--root",
            str(tva_root),
            "day",
            "build",
            "2026-09-14",
            "--executions",
            str(csv),
            "--venue",
            "amp",
        ]
    ) == 0
    assert current_pointer_path(tva_root, date(2026, 9, 14)).is_file()
    assert main(["--root", str(tva_root), "day", "delete", "2026-09-14"]) == 0
    assert not day_dir(tva_root, date(2026, 9, 14)).exists()
    assert audit.is_file()
    assert main(["--root", str(tva_root), "day", "delete", "--all"]) == 0
    assert audit.is_file()
    assert not enabled_from_path(tva_root).exists()


def test_cli_build_flag_off_errors(tva_root: Path, tmp_path: Path, capsys) -> None:
    csv = _write_csv(tmp_path / "exec.csv")
    assert (
        main(
            [
                "--root",
                str(tva_root),
                "day",
                "build",
                "2026-09-14",
                "--executions",
                str(csv),
            ]
        )
        == 1
    )
    err = json.loads(capsys.readouterr().out)
    assert MANIFEST_OFF_ERROR in err["error"]


def test_serve_day_stale_is_http_409(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    enable_day_manifest(tva_root, date(2026, 9, 14))
    client = TestClient(create_app(tva_root))
    response = client.get("/ledger/summary", params={"weeks": 4})
    assert response.status_code == 409
    body = response.json()
    assert DAY_STALE_ERROR in body["detail"]
    assert "2026-09-14" in body["days"]


def test_current_incomplete_does_not_raise(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    a = _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    b = _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    _write_evidence(tva_root, a.id)
    _write_debrief(tva_root, a.id)
    csv = _write_csv(tmp_path / "exec.csv")
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    state = require_fresh(tva_root, date(2026, 9, 14))
    assert state.kind == "current_incomplete"
    assert any(item.session_id == b.id and item.reason == "evidence_pending" for item in state.incomplete)
    status = store.compute_status(tva_root, a.id)
    assert status.day_state == "current_incomplete"


def test_guard_on_missing_pause_checks_aborts_build(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST, ENV_PAUSE_GUARD)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    csv = _write_csv(tmp_path / "exec.csv")
    with pytest.raises(ValueError, match=PAUSE_KEY_ERROR):
        build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")


def test_lock_blocks_second_build(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    lock = day_lock_path(tva_root, date(2026, 9, 14))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{__import__('os').getpid()}\n", encoding="utf-8")
    csv = _write_csv(tmp_path / "exec.csv")
    with pytest.raises(ValueError, match="gesperrt"):
        build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")


def test_nominal_end_uses_utc_plus_duration(tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    start = datetime(2026, 10, 25, 1, 30, tzinfo=CEST)
    _session(
        tva_root,
        "2026-10-25_013000",
        start=start,
        duration_s=7200.0,
    )
    _session(
        tva_root,
        "2026-10-25_120000",
        start=datetime(2026, 10, 25, 12, 0, tzinfo=CET),
        sha256="b" * 64,
    )
    csv = _write_csv(tmp_path / "exec.csv")
    build_day(tva_root, date(2026, 10, 25), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 10, 25))
    assert payload is not None
    clip = next(row for row in payload["clips"] if row["session_id"] == "2026-10-25_013000")
    # UTC 2026-10-24 23:30 + 7200s = 2026-10-25 01:30 UTC = 02:30 CET, not 03:30 CEST.
    assert clip["nominal_end"].startswith("2026-10-25T02:30:00")


def test_flag_off_readers_do_not_load_sessions_or_write(tva_root: Path) -> None:
    dest = config.session_dir(tva_root, "2026-09-14_093000")
    dest.mkdir(parents=True)
    (dest / "session.json").write_text("{not-json", encoding="utf-8")
    require_fresh_store(tva_root)
    require_fresh(tva_root, date(2026, 9, 14))
    ledger_summary(tva_root, weeks=4)
    window_session_ids(tva_root, weeks=4)
    window_trade_facts(tva_root, ["2026-09-14_093000"])
    assert not (tva_root / "days").exists()


def test_empty_lock_is_not_stolen(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    lock = day_lock_path(tva_root, date(2026, 9, 14))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    csv = _write_csv(tmp_path / "exec.csv")
    with pytest.raises(ValueError, match="gesperrt"):
        build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")


def test_dead_lock_is_stolen(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    lock = day_lock_path(tva_root, date(2026, 9, 14))
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("2147483647\n", encoding="utf-8")
    csv = _write_csv(tmp_path / "exec.csv")
    result = build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert result.written
    assert not lock.exists()


def test_coach_window_facts_and_summary_raise_day_stale(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    enable_day_manifest(tva_root, date(2026, 9, 14))
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        coach_session(root=tva_root, provider_name="fake")
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        window_trade_facts(tva_root, ["2026-09-14_093000"])
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        ledger_summary(tva_root, weeks=4)


def test_later_build_does_not_overwrite_enabled_from(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    enable_day_manifest(tva_root, date(2026, 9, 14))
    _session(
        tva_root,
        "2026-09-15_093000",
        start=datetime(2026, 9, 15, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-15_140000",
        start=datetime(2026, 9, 15, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    csv = _write_csv(tmp_path / "exec.csv")
    assert build_day(tva_root, date(2026, 9, 15), executions=csv, venue="amp").written
    assert read_enabled_from(tva_root) == date(2026, 9, 14)


def test_fingerprint_window_uses_csv_written_calendar_date(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    assert "csv_calendar_date" not in FILL_RECORD_COLUMNS
    csv = _write_csv(
        tmp_path / "exec.csv",
        [
            # civil D-1, UTC D-2 23:00 — UTC date would drop this
            _csv_row("2026-09-13T01:00:00+0200", spread="plus02-in"),
            # civil D+2, UTC D+1 23:00 — UTC date would keep this
            _csv_row("2026-09-16T01:00:00+0200", spread="plus02-out"),
            # civil D-2, UTC D-1 04:00 — UTC date would keep this
            _csv_row("2026-09-12T23:00:00-0500", spread="minus05-out"),
            # civil D+1, UTC D+2 04:00 — UTC date would drop this
            _csv_row("2026-09-15T23:00:00-0500", spread="minus05-in"),
            _csv_row("2026-09-14T10:00:00+0000", spread="utc-in"),
        ],
    )
    result = build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert result.written
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    groups = {row["source_group_id"] for row in payload["fingerprint_inputs"]["fill_identities"]}
    assert groups == {"plus02-in", "minus05-in", "utc-in"}
    assert "plus02-out" not in groups
    assert "minus05-out" not in groups
    keys = payload["fingerprint_inputs"]["fill_identities"][0].keys()
    assert "csv_calendar_date" not in keys
    from tradevidanalyser.fills_mirror import load_tradesviz_executions

    fills = load_tradesviz_executions(csv, profile="tradesviz_executions")
    plus02 = next(fill for fill in fills if fill.source_group_id == "plus02-in")
    assert plus02.csv_calendar_date == date(2026, 9, 13)
    assert plus02.timestamp == datetime(2026, 9, 12, 23, 0, tzinfo=timezone.utc)
    assert "20260912T230000Z" in plus02.fill_id


def test_publish_lock_on_built_day_not_on_legacy_or_unbuilt(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _on(monkeypatch, ENV_DAY_MANIFEST)
    a = _session(
        tva_root,
        "2026-09-14_093000",
        start=datetime(2026, 9, 14, 9, 30, tzinfo=VIENNA),
    )
    b = _session(
        tva_root,
        "2026-09-14_140000",
        start=datetime(2026, 9, 14, 14, 0, tzinfo=VIENNA),
        sha256="b" * 64,
    )
    _write_publish_debrief(tva_root, a.id)
    enable_day_manifest(tva_root, date(2026, 9, 14))
    assert not day_dir(tva_root, date(2026, 9, 14)).exists()
    with pytest.raises(DayStale, match=DAY_STALE_ERROR):
        pipeline_publish(a.id, root=tva_root, notion=True)
    assert not day_dir(tva_root, date(2026, 9, 14)).exists()

    lone = _session(
        tva_root,
        "2026-09-16_093000",
        start=datetime(2026, 9, 16, 9, 30, tzinfo=VIENNA),
        sha256="c" * 64,
    )
    _write_publish_debrief(tva_root, lone.id)
    pipeline_publish(lone.id, root=tva_root, notion=True)
    assert not day_dir(tva_root, date(2026, 9, 16)).exists()

    csv = _write_csv(tmp_path / "exec.csv")
    assert build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp").written
    lock = day_lock_path(tva_root, date(2026, 9, 14))
    lock.write_text(f"{__import__('os').getpid()}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="gesperrt"):
        pipeline_publish(a.id, root=tva_root, notion=True)
    lock.unlink()
    _write_publish_debrief(tva_root, b.id)
    pipeline_publish(b.id, root=tva_root, notion=True)
    assert not lock.exists()
    assert day_dir(tva_root, date(2026, 9, 14)).is_dir()
