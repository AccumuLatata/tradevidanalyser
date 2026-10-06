from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.align import align_session
from tradevidanalyser.cli import main
from tradevidanalyser.day_audit import run_day_audit
from tradevidanalyser.evidence import evidence_session
from tradevidanalyser.flags import (
    ENV_DAY_MANIFEST,
    ENV_DAY_PUBLISH,
    ENV_DAY_RULES,
    ENV_EXCLUSIVE_FILLS,
    ENV_PAUSE_GUARD,
    ENV_TRADING_HOURS,
    INVALID_FLAG_SET_ERROR,
    pause_guard_enabled,
    require_allowed_flag_set,
)
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ledger import ledger_db_path
from tradevidanalyser.ocr import OcrRow, clock_text_resolution, write_ocr_parquet
from tradevidanalyser.pause_guard import (
    FAKE_PROVIDER_ERROR,
    FORCE_FLAG_OFF_ERROR,
    MANUAL_SUSPECTED_ERROR,
    PAUSE_DETECTABLE_FROM_HM_S,
    PAUSE_DETECTABLE_FROM_S,
    SCHEMA_VERSION,
    detect_pauses,
    ensure_pause_check,
    key_matches,
    load_pause_check,
    pause_checks_path,
    run_pause_guard,
    sample_times,
    set_clock_reader,
    suspected_unforced,
    write_pause_check,
)
from tradevidanalyser.pipeline import (
    align_session as pipeline_align,
    evidence_session as pipeline_evidence,
    extract_session,
    fills_session,
    ledger_add,
    report_session,
    rules_session,
    transcribe_session,
)
from tradevidanalyser.rules import CLOCK_MAPPED_RULES, evaluate_rules
from tradevidanalyser.publish import payload_from_debrief
from tradevidanalyser.schema import (
    ALIGNMENT_INVALID_REASON,
    Alignment,
    DebriefReport,
    Evidence,
    RecordingInfo,
    RecordingPart,
    SessionRecord,
)

L0_VIDEO_NAME = "2026-05-14 16-03-00.mp4"
L0_SYNTHETIC = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
L0_OCR_OFFSET_S = 1.8

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_JSON_DROP = frozenset({"app_version", "log_line", "created"})
SHAPES = Path(__file__).parent / "fixtures" / "shapes_no_account"
VIENNA = timezone(timedelta(hours=2))
START = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)
L0_START = datetime(2026, 5, 14, 16, 3, tzinfo=VIENNA)


@pytest.fixture(autouse=True)
def _reset_reader() -> None:
    set_clock_reader(None)
    yield
    set_clock_reader(None)


def _drop_l0(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: _drop_l0(value) for key, value in payload.items() if key not in L0_JSON_DROP}
    if isinstance(payload, list):
        return [_drop_l0(item) for item in payload]
    return payload


def _jsonable(value: object) -> object:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _dump_ledger(root: Path, session_id: str) -> dict:
    path = ledger_db_path(root)
    con = duckdb.connect(str(path), read_only=True)
    try:
        payload: dict[str, list] = {}
        for table in ("sessions", "trades", "rule_checks", "events"):
            rows = con.execute(
                f"SELECT * FROM {table} WHERE session_id = ? ORDER BY 1",
                [session_id],
            ).fetchall()
            cols = [col[0] for col in con.description]
            payload[table] = [_jsonable(dict(zip(cols, row, strict=True))) for row in rows]
    finally:
        con.close()
    return payload


def _posix_store_paths(payload: object) -> object:
    """L0 snapshots use forward slashes; Windows ingest may store backslashes."""
    if isinstance(payload, dict):
        out = {key: _posix_store_paths(value) for key, value in payload.items()}
        path = out.get("path")
        if isinstance(path, str) and not path.startswith("http"):
            out["path"] = path.replace("\\", "/")
        return out
    if isinstance(payload, list):
        return [_posix_store_paths(item) for item in payload]
    return payload


def _canonical(payload: object) -> str:
    return json.dumps(_drop_l0(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _json_files_equal(left: Path, right: Path) -> None:
    a = _posix_store_paths(json.loads(left.read_text(encoding="utf-8")))
    b = _posix_store_paths(json.loads(right.read_text(encoding="utf-8")))
    if _canonical(a) != _canonical(b):
        raise AssertionError(f"json differs: {left} vs {right}")


def _parquet_equal(left: Path, right: Path) -> None:
    a = pq.read_table(left)
    b = pq.read_table(right)
    if a.column_names != b.column_names:
        raise AssertionError(f"parquet columns differ: {a.column_names} vs {b.column_names}")
    if not a.equals(b, check_metadata=False):
        raise AssertionError(f"parquet cells differ: {left} vs {right}")


def _session(
    root: Path,
    *,
    session_id: str = "2026-09-11_143000",
    start: datetime = START,
    duration_s: float = 600.0,
    sha256: str = "a" * 64,
    parts: list[RecordingPart] | None = None,
    alignment: Alignment | None = None,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256=sha256,
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename="2026-09-11 14-30-00.mp4",
            parts=parts or [],
        ),
        alignment=alignment,
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _y_reader(start: datetime, y_at, *, fmt: str = "%H:%M:%S"):
    def read(video_t: float) -> str:
        wall = start + timedelta(seconds=float(video_t) + float(y_at(video_t)))
        return wall.strftime(fmt)

    return read


def _step_y(mid: float, high: float, low: float = 0.0):
    def y_at(video_t: float) -> float:
        return high if video_t >= mid else low

    return y_at


def _write_trades(root: Path, session_id: str) -> None:
    table = pa.table(
        {
            "tva_trade_id": pa.array(["T01"], type=pa.string()),
            "entry_timestamp": pa.array(
                [START.astimezone(timezone.utc) + timedelta(seconds=200)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "exit_timestamp": pa.array(
                [START.astimezone(timezone.utc) + timedelta(seconds=220)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def test_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    assert pause_guard_enabled() is False
    for raw in ("", "0", "false", "off", "no"):
        monkeypatch.setenv(ENV_PAUSE_GUARD, raw)
        assert pause_guard_enabled() is False
    for raw in ("1", "true", "YES", "On"):
        monkeypatch.setenv(ENV_PAUSE_GUARD, raw)
        assert pause_guard_enabled() is True


def test_flag_combo_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    monkeypatch.delenv(ENV_DAY_MANIFEST, raising=False)
    for name in (ENV_EXCLUSIVE_FILLS, ENV_DAY_RULES, ENV_DAY_PUBLISH, ENV_TRADING_HOURS):
        monkeypatch.delenv(name, raising=False)
    require_allowed_flag_set()
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    require_allowed_flag_set()
    monkeypatch.setenv(ENV_DAY_MANIFEST, "1")
    require_allowed_flag_set()
    monkeypatch.delenv(ENV_PAUSE_GUARD)
    require_allowed_flag_set()
    monkeypatch.setenv(ENV_EXCLUSIVE_FILLS, "1")
    with pytest.raises(ValueError, match=INVALID_FLAG_SET_ERROR):
        require_allowed_flag_set()


def test_clock_text_resolution() -> None:
    assert clock_text_resolution("16:03:05") == 1
    assert clock_text_resolution("16.03.05") == 1
    assert clock_text_resolution("16:03") == 60
    assert clock_text_resolution("no clock") is None


def test_m5_seconds_300s_suspected(tva_root: Path) -> None:
    record = _session(tva_root)
    check = detect_pauses(
        record, root=tva_root, reader=_y_reader(START, _step_y(300.0, 300.0))
    )
    assert check.pause_check == "suspected"
    assert check.clock_resolution_s == 1
    assert check.pause_detectable_from_s == PAUSE_DETECTABLE_FROM_S
    assert check.pause_total_s is not None
    assert check.pause_total_s > 30
    assert key_matches(check, record)


def test_downward_step_is_unverifiable_not_suspected(tva_root: Path) -> None:
    record = _session(tva_root)

    def y_at(video_t: float) -> float:
        return -20.0 if video_t >= 250.0 else 0.0

    check = detect_pauses(record, root=tva_root, reader=_y_reader(START, y_at))
    assert check.pause_check == "unverifiable"
    assert check.pause_check != "suspected"


def test_m6_unconfirmed_outlier_not_suspected(tva_root: Path) -> None:
    record = _session(tva_root)
    times = sample_times(record.recording.duration_s)
    odd = times[3]

    def y_at(video_t: float) -> float:
        return 60.0 if abs(video_t - odd) < 0.05 else 0.0

    check = detect_pauses(record, root=tva_root, reader=_y_reader(START, y_at))
    assert check.pause_check != "suspected"
    assert any(item.reason == "unconfirmed" for item in check.rejected_samples)


def test_m10_seconds_40s_suspected(tva_root: Path) -> None:
    record = _session(tva_root)
    check = detect_pauses(
        record, root=tva_root, reader=_y_reader(START, _step_y(300.0, 40.0))
    )
    assert check.pause_check == "suspected"
    assert check.clock_resolution_s == 1
    assert check.pause_total_s == pytest.approx(40.0, abs=1.0)


def test_m11_two_confirmed_samples_300s(tva_root: Path) -> None:
    record = _session(tva_root, duration_s=600.0)
    times = sample_times(600.0)
    keep = {round(times[0], 3), round(times[-1], 3)}

    def read(video_t: float) -> str | None:
        nearest = min(times, key=lambda t: abs(t - video_t))
        if round(nearest, 3) not in keep and abs(video_t - nearest) < 0.05:
            return None
        y = 300.0 if nearest >= times[-1] - 1e-9 else 0.0
        wall = START + timedelta(seconds=video_t + y)
        return wall.strftime("%H:%M:%S")

    check = detect_pauses(record, root=tva_root, reader=read)
    assert check.pause_check == "suspected"
    assert len(check.accepted_samples) >= 2
    assert check.pause_total_s is not None
    assert check.pause_total_s == pytest.approx(300.0, abs=5.0)


def test_m13_steps_0_1800_17000(tva_root: Path) -> None:
    record = _session(tva_root, duration_s=3600.0)

    def y_at(video_t: float) -> float:
        if video_t < 800:
            return 0.0
        if video_t < 2000:
            return 1800.0
        return 17000.0

    check = detect_pauses(record, root=tva_root, reader=_y_reader(START, y_at))
    assert check.pause_check == "suspected"
    assert check.clock_resolution_s == 1
    assert check.pause_total_s is not None
    assert check.pause_total_s == pytest.approx(17000.0, abs=5.0)


def test_m16_hm_clean_is_clear(tva_root: Path) -> None:
    record = _session(tva_root)
    check = detect_pauses(
        record, root=tva_root, reader=_y_reader(START, lambda _t: 0.0, fmt="%H:%M")
    )
    assert check.pause_check == "clear"
    assert check.clock_resolution_s == 60
    assert check.pause_detectable_from_s == PAUSE_DETECTABLE_FROM_HM_S


def test_m17_hm_300s_suspected(tva_root: Path) -> None:
    record = _session(tva_root)
    check = detect_pauses(
        record,
        root=tva_root,
        reader=_y_reader(START, _step_y(300.0, 300.0), fmt="%H:%M"),
    )
    assert check.pause_check == "suspected"
    assert check.clock_resolution_s == 60
    assert check.pause_detectable_from_s == PAUSE_DETECTABLE_FROM_HM_S


def test_m17b_hm_90s_clear(tva_root: Path) -> None:
    record = _session(tva_root)
    check = detect_pauses(
        record,
        root=tva_root,
        reader=_y_reader(START, _step_y(200.0, 90.0), fmt="%H:%M"),
    )
    assert check.pause_check == "clear"
    assert check.clock_resolution_s == 60
    assert check.pause_detectable_from_s == PAUSE_DETECTABLE_FROM_HM_S
    assert check.pause_total_s is not None
    assert check.pause_total_s <= 120


def test_m17c_seconds_file_drops_hm(tva_root: Path) -> None:
    record = _session(tva_root)
    times = sample_times(record.recording.duration_s)
    hm_centers = {round(times[2], 3), round(times[5], 3)}

    def read(video_t: float) -> str:
        nearest = min(times, key=lambda t: abs(t - video_t))
        wall = START + timedelta(seconds=video_t)
        if round(nearest, 3) in hm_centers:
            sawed = wall + timedelta(seconds=40)
            return sawed.strftime("%H:%M")
        return wall.strftime("%H:%M:%S")

    check = detect_pauses(record, root=tva_root, reader=read)
    assert check.pause_check == "clear"
    assert check.clock_resolution_s == 1
    assert any(item.reason == "resolution_mismatch" for item in check.rejected_samples)
    assert all(item.resolution_s == 1 for item in check.accepted_samples)


def test_m20_fake_provider_aborts(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    with pytest.raises(ValueError, match=FAKE_PROVIDER_ERROR):
        run_pause_guard(record, root=tva_root)
    assert not pause_checks_path(tva_root, record.id).is_file()


def test_align_flag_off_does_not_write_pause_checks(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    record = _session(tva_root)
    align_session(record.id, root=tva_root)
    assert not pause_checks_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "filename"


def test_align_flag_off_cli_ignores_leftover_pause_checks(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    record = _session(tva_root)
    leftover = detect_pauses(record, root=tva_root, reader=_y_reader(START, lambda _t: 0.0))
    write_pause_check(tva_root, leftover)
    assert main(["--root", str(tva_root), "align", record.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["method"] == "filename"
    assert "pause_check" not in payload
    assert "override" not in payload


def test_align_suspected_writes_invalid(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, _step_y(300.0, 300.0)))
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "invalid"
    assert alignment.offset_s == 0.0
    assert alignment.confidence == 0.0
    check = load_pause_check(tva_root, record.id)
    assert check is not None
    assert check.pause_check == "suspected"
    assert check.override is None
    assert key_matches(check, store.load_session(tva_root, record.id))


def test_force_flag_off_is_error(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    record = _session(tva_root)
    with pytest.raises(ValueError, match=FORCE_FLAG_OFF_ERROR):
        align_session(record.id, root=tva_root, force=True)
    assert main(["--root", str(tva_root), "align", record.id, "--force"]) == 1


def test_force_writes_fit_and_override(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, _step_y(300.0, 300.0)))
    alignment = align_session(record.id, root=tva_root, force=True)
    assert alignment.method == "filename"
    check = load_pause_check(tva_root, record.id)
    assert check is not None
    assert check.pause_check == "suspected"
    assert check.override == "force"


def test_manual_offset_suspected_requires_force(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, _step_y(300.0, 300.0)))
    before = store.session_json_path(tva_root, record.id).read_bytes()
    with pytest.raises(ValueError, match=MANUAL_SUSPECTED_ERROR):
        align_session(record.id, root=tva_root, manual_offset=3.5)
    assert store.session_json_path(tva_root, record.id).read_bytes() == before
    alignment = align_session(record.id, root=tva_root, manual_offset=3.5, force=True)
    assert alignment.method == "manual"
    assert alignment.offset_s == pytest.approx(3.5)
    assert alignment.drift_s_per_h == 0.0
    check = load_pause_check(tva_root, record.id)
    assert check is not None
    assert check.override == "force"


def test_suspected_check_without_invalid_does_not_filename_map(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Plan §1.10 / §3.4: pause_checks suspected + alignment None must not map."""
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    set_clock_reader(_y_reader(START, _step_y(300.0, 300.0)))
    check = detect_pauses(
        record, root=tva_root, reader=_y_reader(START, _step_y(300.0, 300.0))
    )
    assert check.pause_check == "suspected"
    assert suspected_unforced(check)
    write_pause_check(tva_root, check)
    assert store.load_session(tva_root, record.id).alignment is None
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert result.reason == ALIGNMENT_INVALID_REASON
    assert not store.evidence_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "invalid"


def test_corrupt_override_is_missing_not_trusted(tva_root: Path) -> None:
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, lambda _t: 0.0))
    check = detect_pauses(record, root=tva_root, reader=_y_reader(START, lambda _t: 0.0))
    payload = check.as_dict()
    payload["override"] = "please"
    store.write_json(pause_checks_path(tva_root, record.id), payload)
    assert load_pause_check(tva_root, record.id, allow_fake=True) is None
    payload["override"] = None
    payload["schema_version"] = "2"
    store.write_json(pause_checks_path(tva_root, record.id), payload)
    assert load_pause_check(tva_root, record.id, allow_fake=True) is None
    payload["schema_version"] = SCHEMA_VERSION
    store.write_json(pause_checks_path(tva_root, record.id), payload)
    loaded = load_pause_check(tva_root, record.id, allow_fake=True)
    assert loaded is not None
    assert loaded.schema_version == SCHEMA_VERSION


def test_m5_no_evidence_windows(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    store.write_json(
        store.evidence_path(tva_root, record.id),
        Evidence(provider="fake", model="none", prompt_version="x", session_id=record.id).model_dump(
            mode="json"
        ),
    )
    set_clock_reader(_y_reader(START, _step_y(300.0, 300.0)))
    align_session(record.id, root=tva_root)
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert result.reason == ALIGNMENT_INVALID_REASON
    assert not store.evidence_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "invalid"
    checks = {
        item.rule: item
        for item in evaluate_rules([], alignment=loaded.alignment)
    }
    for rule in CLOCK_MAPPED_RULES:
        assert checks[rule].reason == ALIGNMENT_INVALID_REASON
    assert checks["R-BIAS"].reason != ALIGNMENT_INVALID_REASON


def test_res60_evidence_trades_alignment_low(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(
        tva_root,
        alignment=Alignment(
            offset_s=0.0,
            drift_s_per_h=0.0,
            confidence=1.0,
            method="ocr_clock",
        ),
    )
    _write_trades(tva_root, record.id)
    set_clock_reader(_y_reader(START, lambda _t: 0.0, fmt="%H:%M"))
    run_pause_guard(record, root=tva_root, apply_alignment=True)
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "ok"
    evidence = Evidence.model_validate(store.read_json(store.evidence_path(tva_root, record.id)))
    assert evidence.trades
    for trade in evidence.trades:
        assert trade.alignment == "low"


def test_invalidate_downstream_deletes_pause_checks(tva_root: Path) -> None:
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, lambda _t: 0.0))
    check = detect_pauses(record, root=tva_root, reader=_y_reader(START, lambda _t: 0.0))
    write_pause_check(tva_root, check)
    assert pause_checks_path(tva_root, record.id).is_file()
    store.save_session(
        tva_root,
        record.model_copy(
            update={
                "alignment": Alignment(
                    offset_s=0.0,
                    drift_s_per_h=0.0,
                    confidence=0.6,
                    method="filename",
                )
            }
        ),
    )
    store.invalidate_downstream(tva_root, record.id)
    assert not pause_checks_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is None


def test_reingest_suspected_blocks_evidence_until_new_key(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, write_video
) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    video = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=4.0)
    record = ingest(video, root=tva_root)
    set_clock_reader(_y_reader(START, _step_y(2.0, 300.0)))
    align_session(record.id, root=tva_root)
    assert load_pause_check(tva_root, record.id) is not None
    assert store.load_session(tva_root, record.id).alignment.method == "invalid"
    write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=8.0)
    updated = ingest(video, root=tva_root)
    assert updated.recording.sha256 != record.recording.sha256
    assert not pause_checks_path(tva_root, record.id).is_file()
    set_clock_reader(None)
    with pytest.raises(ValueError, match=FAKE_PROVIDER_ERROR):
        evidence_session(updated.id, root=tva_root)
    set_clock_reader(_y_reader(START, _step_y(2.0, 300.0)))
    ensure_pause_check(store.load_session(tva_root, updated.id), root=tva_root)
    check = load_pause_check(tva_root, updated.id)
    assert check is not None
    assert key_matches(check, store.load_session(tva_root, updated.id))


def test_sample_clocks_audit_json_only(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    set_clock_reader(_y_reader(START, lambda _t: 0.0))
    result = run_day_audit(
        root=tva_root,
        date_from="2026-09-11",
        date_to="2026-09-11",
        sample_clocks=True,
    )
    assert "clock_samples" in result.payload
    samples = result.payload["clock_samples"]
    assert samples
    assert samples[0]["session_id"] == record.id
    assert samples[0]["pause_check"] == "clear"
    assert not pause_checks_path(tva_root, record.id).is_file()
    raw = json.loads((tva_root / "days" / "audit.json").read_text(encoding="utf-8"))
    assert "clock_samples" in raw
    monkeypatch.delenv(ENV_PAUSE_GUARD, raising=False)
    assert store.load_session(tva_root, record.id).alignment is None


def test_pause_checks_delete_is_named_command(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(
        tva_root,
        alignment=Alignment(
            offset_s=1.0, drift_s_per_h=0.0, confidence=1.0, method="ocr_clock"
        ),
    )
    set_clock_reader(_y_reader(START, lambda _t: 0.0))
    run_pause_guard(record, root=tva_root, apply_alignment=False)
    assert pause_checks_path(tva_root, record.id).is_file()
    assert main(["--root", str(tva_root), "pause-checks", "delete", record.id]) == 0
    assert not pause_checks_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "ocr_clock"


def test_shapes_no_account_fixture() -> None:
    assert SHAPES.is_dir()
    parts = sorted(SHAPES.glob("*.mp4"))
    assert len(parts) == 2
    assert parts[0].name.startswith("2026-10-01 09-00-")
    csv_path = SHAPES / "executions.csv"
    text = csv_path.read_text(encoding="utf-8")
    assert "+0000" in text
    assert "+0200" in text
    assert "account" not in text.lower()
    clocks = json.loads((SHAPES / "clocks.json").read_text(encoding="utf-8"))
    assert any(":" in item and item.count(":") == 2 for item in clocks["hhmmss"])
    assert any(item.count(":") == 1 for item in clocks["hhmm"])


def test_l0_with_guard_on_matches_snapshot(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    inputs = L0_DIR / "inputs"
    video = inputs / L0_VIDEO_NAME
    assert video.is_file()

    def _clock_rows() -> list[OcrRow]:
        rows: list[OcrRow] = []
        for i in range(10):
            video_t = round(i * 0.2, 3)
            wall = L0_START + timedelta(seconds=video_t + L0_OCR_OFFSET_S)
            rows.append(
                OcrRow(
                    t=video_t,
                    roi="clock",
                    text=wall.strftime("%H:%M:%S"),
                    confidence=0.95,
                    parsed=wall.isoformat(),
                )
            )
        return rows

    def _run(root: Path, *, with_ocr: bool) -> str:
        config.ensure_layout(root)
        dest_video = tmp_path / root.name / L0_VIDEO_NAME
        dest_video.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, dest_video)
        record = ingest(dest_video, root=root)
        set_clock_reader(_y_reader(L0_START, lambda _t: 0.0))
        transcribe_session(record.id, root=root)
        extract_session(record.id, root=root)
        if with_ocr:
            write_ocr_parquet(store.ocr_path(root, record.id), _clock_rows())
        pipeline_align(record.id, root=root)
        fills_session(record.id, root=root, executions=L0_SYNTHETIC, venue="amp")
        pipeline_evidence(record.id, root=root)
        rules_session(record.id, root=root)
        report_session(record.id, root=root)
        ledger_add(record.id, root=root)
        check = load_pause_check(root, record.id)
        assert check is not None
        assert check.pause_check == "clear"
        assert check.clock_resolution_s == 1
        assert check.pause_detectable_from_s == PAUSE_DETECTABLE_FROM_S
        assert not (root / "days").exists()
        return record.id

    ocr_root = tva_root / "ocr"
    fn_root = tva_root / "filename"
    ocr_id = _run(ocr_root, with_ocr=True)
    fn_id = _run(fn_root, with_ocr=False)
    for name, root, session_id in (
        ("l0-ocr", ocr_root, ocr_id),
        ("l0-filename", fn_root, fn_id),
    ):
        src = L0_DIR / name
        dest = config.session_dir(root, session_id)
        for filename in (
            "session.json",
            "fills.parquet",
            "trades.parquet",
            "evidence.json",
            "rules.json",
            "debrief.md",
            "debrief.json",
        ):
            _json_or_parquet(dest / filename, src / filename)
        got_ledger = _dump_ledger(root, session_id)
        want_ledger = json.loads((src / "ledger.json").read_text(encoding="utf-8"))
        if _canonical(got_ledger) != _canonical(want_ledger):
            raise AssertionError(f"ledger differs for {name}")
        record = store.load_session(root, session_id)
        report = DebriefReport.model_validate(
            store.read_json(store.debrief_json_path(root, session_id))
        )
        payload = payload_from_debrief(record, report)
        got_notion = {
            "title": payload.title,
            "summaries": payload.summaries,
            "learnings": list(payload.learnings),
        }
        want_notion = json.loads((src / "notion_payload.json").read_text(encoding="utf-8"))
        assert _canonical(got_notion) == _canonical(want_notion)
        check = load_pause_check(root, session_id)
        assert check is not None
        assert check.pause_check == "clear"


def _json_or_parquet(left: Path, right: Path) -> None:
    if left.suffix == ".parquet":
        _parquet_equal(left, right)
        return
    if left.suffix in {".json", ".md"}:
        if left.suffix == ".md":
            assert left.read_text(encoding="utf-8") == right.read_text(encoding="utf-8")
            return
        _json_files_equal(left, right)
        return
    assert left.read_bytes() == right.read_bytes()
