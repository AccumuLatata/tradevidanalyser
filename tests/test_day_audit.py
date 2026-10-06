from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.day_audit import (
    SHIFT_HOURS,
    TRUE_TIME_RULE,
    TZ_ASSUMPTION_CSV,
    TZ_ASSUMPTION_WAIVED,
    DayAuditError,
    audit_path,
    fill_identity,
    run_day_audit,
)
from tradevidanalyser.fills import fills_table, session_utc_window
from tradevidanalyser.fills_mirror import FillRecord
from tradevidanalyser.ingest import SPLIT_GAP_S
from tradevidanalyser.pipeline import fills_session
from tradevidanalyser.schema import RecordingInfo, RecordingPart, SessionRecord

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_VARIANTS = ("l0-ocr", "l0-filename")
L0_SESSION_FILES = (
    "session.json",
    "fills.parquet",
    "trades.parquet",
    "evidence.json",
    "rules.json",
    "debrief.md",
    "debrief.json",
    "ledger.json",
    "notion_payload.json",
)

CSV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)


def _session(
    root: Path,
    session_id: str,
    *,
    start: str,
    duration_s: float = 3600.0,
    filename: str | None = None,
    parts: list[RecordingPart] | None = None,
    path: str | None = None,
) -> SessionRecord:
    name = filename or f"{session_id[:10]} {session_id[11:13]}-{session_id[13:15]}-{session_id[15:17]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=path or f"recordings/{name}",
            sha256="0" * 64,
            start_wallclock_vienna=start,
            duration_s=duration_s,
            filename=name,
            parts=parts or [],
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _csv_row(
    ts: str,
    *,
    symbol: str = "MNQM26",
    side: str = "buy",
    price: str = "21000.0",
    qty: str = "1.0",
    asset: str = "future",
    spread: str = "g1",
) -> str:
    if symbol.startswith("MNQ"):
        underlying = "MNQ"
    elif symbol.startswith("MES"):
        underlying = "MES"
    else:
        underlying = symbol[:3]
    return (
        f"{ts},{symbol},{side},USD,{underlying},{asset},{price},{qty},"
        f"0,0,N/A,N/A,,,{spread}"
    )


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _extra_fill(*, instrument: str, timestamp: datetime) -> FillRecord:
    return FillRecord(
        fill_id="tv:other-instrument",
        source="tradesviz",
        source_group_id="other",
        instrument=instrument,
        contract_month="SEP",
        contract_year=2026,
        side="buy",
        qty=1,
        price=5800.0,
        timestamp=timestamp,
        session_date=timestamp.date(),
        entry_kind="imported",
        tags=(),
        notes_text="",
        declared_stop=None,
        declared_target=None,
        flags=(),
    )


def test_audit_classes_and_windows(tva_root: Path, tmp_path: Path) -> None:
    a = _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
        duration_s=3600.0,
    )
    b = _session(
        tva_root,
        "2026-09-14_104500",
        start="2026-09-14T10:45:00+02:00",
        duration_s=1800.0,
    )
    missing = _session(
        tva_root,
        "2026-09-15_090000",
        start="2026-09-15T09:00:00+02:00",
        duration_s=1800.0,
    )
    _session(
        tva_root,
        "2026-09-10_090000",
        start="2026-09-10T09:00:00+02:00",
        duration_s=1800.0,
    )
    csv = _write_csv(
        tmp_path / "executions.csv",
        [
            _csv_row("2026-09-14T07:45:00+0000", side="buy", spread="onlyA", price="21000.0"),
            _csv_row("2026-09-14T07:45:10+0000", side="sell", spread="onlyA", price="21001.0"),
            _csv_row("2026-09-14T08:30:00+0000", side="buy", spread="both", price="21100.0"),
            _csv_row("2026-09-14T08:30:10+0000", side="sell", spread="both", price="21101.0"),
            _csv_row("2026-09-14T12:00:00+0000", side="buy", spread="out", price="21200.0"),
            _csv_row("2026-09-14T12:00:10+0000", side="sell", spread="out", price="21201.0"),
            _csv_row(
                "2026-09-14T07:50:00+0000",
                symbol="MNQ",
                side="sell",
                price="20900.0",
                asset="stock",
                spread="manualA",
            ),
            _csv_row("2026-09-15T07:10:00+0000", side="buy", spread="missingSess", price="21300.0"),
            _csv_row("2026-09-15T07:10:10+0000", side="sell", spread="missingSess", price="21301.0"),
        ],
    )
    fills_session(a.id, root=tva_root, executions=csv, venue="amp")
    fills_session(b.id, root=tva_root, executions=csv, venue="amp")
    extra_ts = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    extra_table = fills_table([_extra_fill(instrument="ES", timestamp=extra_ts)], venue="amp")
    existing = pq.read_table(store.fills_path(tva_root, a.id))
    pq.write_table(pa.concat_tables([existing, extra_table]), store.fills_path(tva_root, a.id))

    before = {
        path: path.stat().st_mtime_ns
        for path in (
            store.session_json_path(tva_root, a.id),
            store.fills_path(tva_root, a.id),
            store.trades_path(tva_root, a.id),
            store.session_json_path(tva_root, missing.id),
        )
    }

    result = run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-15",
        executions=csv,
        clock_note="ROI HH:MM:SS; large clock HH:MM:SS",
    )
    assert result.fills_match == "ok"
    assert result.tz_assumption == TZ_ASSUMPTION_CSV
    assert result.loader == "unknown"
    dest = audit_path(tva_root)
    assert dest.is_file()
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["tz_assumption"] == TZ_ASSUMPTION_CSV
    assert data["tz_assumption"] != TZ_ASSUMPTION_WAIVED
    assert data["clock_note"] == "ROI HH:MM:SS; large clock HH:MM:SS"
    assert data["visible_fills"] == []
    assert data["mtime_pause_hint"] == "unverified"
    ids = [row["session_id"] for row in data["sessions"]]
    assert ids == ["2026-09-14_100000", "2026-09-14_104500", "2026-09-15_090000"]
    assert data["sessions_without_fills_run"] == ["2026-09-15_090000"]
    assert data["classes"]["in_existing_parquet"] == 5
    assert data["classes"]["filtered_manual"] == 1
    assert data["classes"]["other_instrument"] == 1
    assert data["classes"]["other_account"] == "not_in_schema"
    assert data["classes"]["session_without_fills_run"] == 1
    assert data["window"]["tz_assumption"] == TZ_ASSUMPTION_CSV
    assert data["window"]["in_one"] == 5
    assert data["window"]["in_many"] == 2
    assert data["window"]["outside_all"] == 2
    assert data["window"]["fills_outside_window"]["2026-09-14_100000"] == 4
    venues = {row["session_id"]: row["venue"] for row in data["sessions"]}
    assert venues["2026-09-14_100000"] == "amp"
    assert venues["2026-09-15_090000"] is None
    missing_row = next(row for row in data["sessions"] if row["session_id"] == missing.id)
    assert missing_row["class"] == "session_without_fills_run"
    assert missing_row["has_fills_parquet"] is False

    for path, stamp in before.items():
        assert path.stat().st_mtime_ns == stamp


def test_missing_csv_is_not_run(tva_root: Path) -> None:
    _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
    )
    result = run_day_audit(root=tva_root, date_from="2026-09-14", date_to="2026-09-14")
    assert result.fills_match == "not_run"
    data = json.loads(audit_path(tva_root).read_text(encoding="utf-8"))
    assert data["fills_match"] == "not_run"
    assert data["window"] is None
    assert data["shifts"] is None
    assert data["executions"] is None
    assert data["classes"]["other_account"] == "not_in_schema"


def test_shift_sign_is_csv_plus_delta(tva_root: Path, tmp_path: Path) -> None:
    record = _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
        duration_s=3600.0,
    )
    start, end = session_utc_window(record)
    assert end == datetime(2026, 9, 14, 9, 30, tzinfo=timezone.utc)
    csv = _write_csv(
        tmp_path / "shift.csv",
        [
            _csv_row("2026-09-14T09:45:00+0000", side="buy", spread="shift", price="21000.0"),
            _csv_row("2026-09-14T09:45:10+0000", side="sell", spread="shift", price="21001.0"),
        ],
    )
    result = run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-14",
        executions=csv,
    )
    shifts = result.payload["shifts"]
    assert shifts["true_time"] == TRUE_TIME_RULE
    assert set(shifts["by_delta_hours"]) == {f"{d:+d}" for d in SHIFT_HOURS}
    unshifted = result.payload["window"]
    assert unshifted["outside_all"] == 2
    assert unshifted["in_one"] == 0
    minus_one = shifts["by_delta_hours"]["-1"]
    plus_one = shifts["by_delta_hours"]["+1"]
    assert minus_one["delta_hours"] == -1
    assert plus_one["delta_hours"] == 1
    assert minus_one["in_one"] == 2
    assert minus_one["outside_all"] == 0
    assert plus_one["outside_all"] == 2
    csv_ts = datetime(2026, 9, 14, 9, 45, tzinfo=timezone.utc)
    assert csv_ts + timedelta(hours=-1) < end
    assert start <= csv_ts + timedelta(hours=-1) <= end
    assert csv_ts + timedelta(hours=1) > end


def test_stitch_and_mtime_hint(tva_root: Path, tmp_path: Path) -> None:
    rec_dir = tva_root / "recordings"
    rec_dir.mkdir(parents=True, exist_ok=True)
    part1 = rec_dir / "2026-09-14 10-00-00.mp4"
    part2 = rec_dir / "2026-09-14 10-00-03.mp4"
    part1.write_bytes(b"fake-mp4-a")
    part2.write_bytes(b"fake-mp4-b")
    start = datetime(2026, 9, 14, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    file_end = start + timedelta(seconds=6)
    stamp = file_end.timestamp()
    import os

    os.utime(part2, (stamp, stamp))
    stitched = _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
        duration_s=5.0,
        filename=part1.name,
        path=f"recordings/{part1.name}",
        parts=[
            RecordingPart(
                path=f"recordings/{part1.name}",
                sha256="a" * 64,
                duration_s=3.0,
                offset_s=0.0,
                filename=part1.name,
            ),
            RecordingPart(
                path=f"recordings/{part2.name}",
                sha256="b" * 64,
                duration_s=2.0,
                offset_s=3.0,
                filename=part2.name,
            ),
        ],
    )
    later = _session(
        tva_root,
        "2026-09-14_100020",
        start="2026-09-14T10:00:20+02:00",
        duration_s=10.0,
    )
    data = run_day_audit(
        root=tva_root, date_from="2026-09-14", date_to="2026-09-14"
    ).payload
    row = next(item for item in data["sessions"] if item["session_id"] == stitched.id)
    assert row["stitched_gap_s"] == pytest.approx(0.0)
    assert row["mtime_pause_hint"] == "unverified"
    assert row["mtime_pause_hint_s"] == pytest.approx(1.0, abs=1.0)
    gaps = data["unstitched_gaps"]
    assert len(gaps) == 1
    assert gaps[0]["left_session_id"] == stitched.id
    assert gaps[0]["right_session_id"] == later.id
    assert gaps[0]["gap_s"] > SPLIT_GAP_S


def test_cli_day_audit_and_no_sample_clocks(tva_root: Path, tmp_path: Path, capsys) -> None:
    _session(tva_root, "2026-09-14_100000", start="2026-09-14T10:00:00+02:00")
    csv = _write_csv(
        tmp_path / "one.csv",
        [
            _csv_row("2026-09-14T08:10:00+0000", side="buy"),
            _csv_row("2026-09-14T08:10:10+0000", side="sell"),
        ],
    )
    assert (
        main(
            [
                "--root",
                str(tva_root),
                "day",
                "audit",
                "--from",
                "2026-09-14",
                "--to",
                "2026-09-14",
                "--executions",
                str(csv),
                "--loader",
                "mirror",
                "--clock-note",
                "manual frame look",
            ]
        )
        == 0
    )
    out = json.loads(capsys.readouterr().out)
    assert out["fills_match"] == "ok"
    assert out["loader"] == "mirror"
    assert out["clock_note"] == "manual frame look"
    assert (tva_root / "days" / "audit.json").is_file()
    with pytest.raises(SystemExit):
        main(
            [
                "--root",
                str(tva_root),
                "day",
                "audit",
                "--sample-clocks",
            ]
        )


def test_default_to_is_vienna_today(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date as date_cls

    _session(tva_root, "2026-10-06_090000", start="2026-10-06T09:00:00+02:00")
    monkeypatch.setattr(
        "tradevidanalyser.day_audit.vienna_today", lambda: date_cls(2026, 10, 6)
    )
    result = run_day_audit(root=tva_root, date_from="2026-09-14")
    assert result.date_to == date_cls(2026, 10, 6)
    assert [row["session_id"] for row in result.payload["sessions"]] == ["2026-10-06_090000"]


def test_fill_identity_ignores_fill_id() -> None:
    ts = datetime(2026, 9, 14, 8, 0, tzinfo=timezone.utc)
    a = _extra_fill(instrument="MNQ", timestamp=ts)
    b = FillRecord(
        fill_id="tv:different-id",
        source="tradesviz",
        source_group_id="other",
        instrument="MNQ",
        contract_month="SEP",
        contract_year=2026,
        side="buy",
        qty=1,
        price=5800.0,
        timestamp=ts,
        session_date=ts.date(),
        entry_kind="imported",
        tags=(),
        notes_text="",
        declared_stop=None,
        declared_target=None,
        flags=(),
    )
    assert fill_identity(a) == fill_identity(b)


def test_l0_snapshot_exists_without_days() -> None:
    for name in L0_VARIANTS:
        folder = L0_DIR / name
        assert folder.is_dir(), folder
        for filename in L0_SESSION_FILES:
            path = folder / filename
            assert path.is_file(), path
        assert not (folder / "days").exists()
        session = json.loads((folder / "session.json").read_text(encoding="utf-8"))
        assert session["alignment"]["method"] in {"ocr_clock", "filename"}
        if name == "l0-ocr":
            assert session["alignment"]["method"] == "ocr_clock"
        else:
            assert session["alignment"]["method"] == "filename"
        evidence = json.loads((folder / "evidence.json").read_text(encoding="utf-8"))
        if name == "l0-ocr":
            for trade in evidence["trades"]:
                assert trade.get("alignment") is None
        payload = json.loads((folder / "notion_payload.json").read_text(encoding="utf-8"))
        assert {"title", "summaries", "learnings"} <= set(payload)
        ledger = json.loads((folder / "ledger.json").read_text(encoding="utf-8"))
        assert "sessions" in ledger
        assert "trades" in ledger


def test_sessions_sorted_by_utc_not_ingest_order(tva_root: Path) -> None:
    later = _session(
        tva_root,
        "2026-09-14_110000",
        start="2026-09-14T11:00:00+02:00",
    )
    earlier = _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
    )
    data = run_day_audit(
        root=tva_root, date_from="2026-09-14", date_to="2026-09-14"
    ).payload
    assert [row["session_id"] for row in data["sessions"]] == [earlier.id, later.id]


def test_unparseable_filename_is_not_empty_prefix(tva_root: Path) -> None:
    _session(
        tva_root,
        "2026-09-14_100000",
        start="2026-09-14T10:00:00+02:00",
        duration_s=60.0,
    )
    _session(
        tva_root,
        "2026-09-14_100200",
        start="2026-09-14T10:02:00+02:00",
        duration_s=60.0,
        filename="notes.mp4",
        path="recordings/notes.mp4",
    )
    data = run_day_audit(
        root=tva_root, date_from="2026-09-14", date_to="2026-09-14"
    ).payload
    assert data["unstitched_gaps"] == []


def test_loader_flag_selects_load_fills(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _session(tva_root, "2026-09-14_100000", start="2026-09-14T10:00:00+02:00")
    csv = _write_csv(
        tmp_path / "emptyish.csv",
        [_csv_row("2026-09-14T08:10:00+0000"), _csv_row("2026-09-14T08:10:10+0000", side="sell")],
    )
    seen: list[bool | None] = []

    def fake_load(path, *, prefer_import=None):
        seen.append(prefer_import)
        from tradevidanalyser.fills import load_fills as real_load

        return real_load(path, prefer_import=False)

    monkeypatch.setattr("tradevidanalyser.day_audit.load_fills", fake_load)
    run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-14",
        executions=csv,
        loader="mirror",
    )
    run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-14",
        executions=csv,
        loader="import",
    )
    run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-14",
        executions=csv,
        loader="unknown",
    )
    assert seen == [False, True, None]


def test_refuses_to_overwrite_tz_assumption_waived(tva_root: Path) -> None:
    _session(tva_root, "2026-09-14_100000", start="2026-09-14T10:00:00+02:00")
    dest = audit_path(tva_root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps({"tz_assumption": TZ_ASSUMPTION_WAIVED, "visible_fills": [{"n": 1}]}),
        encoding="utf-8",
    )
    before = dest.read_text(encoding="utf-8")
    stamp = dest.stat().st_mtime_ns
    with pytest.raises(DayAuditError, match="waived"):
        run_day_audit(root=tva_root, date_from="2026-09-14", date_to="2026-09-14")
    assert dest.read_text(encoding="utf-8") == before
    assert dest.stat().st_mtime_ns == stamp


def test_re_run_preserves_visible_fills_and_clock_note(tva_root: Path) -> None:
    _session(tva_root, "2026-09-14_100000", start="2026-09-14T10:00:00+02:00")
    first = run_day_audit(
        root=tva_root,
        date_from="2026-09-14",
        date_to="2026-09-14",
        clock_note="ROI HH:MM:SS",
    )
    dest = audit_path(tva_root)
    payload = json.loads(dest.read_text(encoding="utf-8"))
    payload["visible_fills"] = [
        {
            "session_id": "2026-09-14_100000",
            "video_t": 12.0,
            "csv_timestamp": "2026-09-14T08:10:00+00:00",
        }
    ]
    dest.write_text(json.dumps(payload), encoding="utf-8")
    again = run_day_audit(root=tva_root, date_from="2026-09-14", date_to="2026-09-14")
    data = json.loads(dest.read_text(encoding="utf-8"))
    assert data["tz_assumption"] == TZ_ASSUMPTION_CSV
    assert data["tz_assumption"] != TZ_ASSUMPTION_WAIVED
    assert data["visible_fills"] == payload["visible_fills"]
    assert data["clock_note"] == "ROI HH:MM:SS"
    assert again.clock_note == first.clock_note


def test_trades_missing_identity_columns_fail_closed(tva_root: Path) -> None:
    record = _session(
        tva_root, "2026-09-14_100000", start="2026-09-14T10:00:00+02:00"
    )
    pq.write_table(
        pa.table({"trade_id": pa.array(["x"], type=pa.string())}),
        store.trades_path(tva_root, record.id),
    )
    with pytest.raises(DayAuditError, match="trades.parquet missing identity columns"):
        run_day_audit(root=tva_root, date_from="2026-09-14", date_to="2026-09-14")


def test_l0_parity_helper_roundtrip() -> None:
    from l0_parity import json_files_equal, l0_variant_dir

    ocr = l0_variant_dir("l0-ocr") / "session.json"
    filename = l0_variant_dir("l0-filename") / "session.json"
    json_files_equal(ocr, ocr)
    with pytest.raises(AssertionError, match="json differs"):
        json_files_equal(ocr, filename)
