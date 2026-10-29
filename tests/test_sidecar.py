"""PR-40: sidecar reader and time_anchor.json. Synthetic fixtures only."""

from __future__ import annotations

import json
import warnings
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tradevidanalyser import config, store
from tradevidanalyser.flags import ENV_PAUSE_GUARD, ENV_SIDECAR
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pause_guard import (
    PauseCheck,
    ensure_pause_check,
    load_pause_check,
    run_pause_guard,
    write_pause_check,
)
from tradevidanalyser.schema import RecordingInfo, SessionRecord
from tradevidanalyser.sidecar import (
    EVENT_FILE_CHANGED,
    EVENT_PAUSE,
    EVENT_RECORD_START,
    EVENT_RECORD_STOP,
    EVENT_RESUME,
    FALLBACK_WARNING,
    FIRST_FRAME_OFFSET_EXPECTED_S,
    METHOD_SIDECAR,
    OFFSET_SOURCE_FALLBACK,
    REASON_FIRST_FRAME,
    REASON_INVALID_JSON,
    REASON_SEGMENT_INVALID,
    SOURCE_FIXED,
    SOURCE_MEASURED,
    STATUS_INCOMPLETE,
    STATUS_INVALID,
    STATUS_NO_SIDECAR,
    STATUS_PARTIAL,
    STATUS_SIDECAR_MISMATCH,
    STATUS_VERIFIED,
    SidecarFallbackWarning,
    SidecarFill,
    build_time_anchor,
    load_time_anchor,
    parse_sidecar_jsonl,
    sidecar_replaces_detector,
    time_anchor_path,
    write_session_time_anchor,
    write_time_anchor,
    _event_from_raw,
)

VIENNA = ZoneInfo("Europe/Vienna")
T0 = datetime(2026, 10, 12, 14, 0, 0, tzinfo=VIENNA)
OFFSET = FIRST_FRAME_OFFSET_EXPECTED_S
VIDEO = "2026-10-12 14-00-00.mp4"
SESSION_ID = "2026-10-12_140000"


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _event(name: str, *, wall: datetime, video_s: float, file: str | None = None) -> dict:
    row = {"event": name, "wall": wall.isoformat(), "video_s": video_s}
    if file:
        row["file"] = file
    return row


def _wall(video_s: float, *, paused: float = 0.0) -> datetime:
    return T0 + timedelta(seconds=video_s + OFFSET + paused)


def _verified_events(*, resume_offsets: tuple[float, ...] = (0.58,)) -> list[dict]:
    """One or more pauses; media 50 s per section; 5 s wall pause each."""
    rows = [_event(EVENT_RECORD_START, wall=T0, video_s=0.0, file=VIDEO)]
    video_s = 0.0
    paused = 0.0
    for index, resume_off in enumerate(resume_offsets, start=1):
        video_s += 50.0
        rows.append(_event(EVENT_PAUSE, wall=_wall(video_s, paused=paused), video_s=video_s))
        paused += 5.0
        rows.append(
            _event(
                EVENT_RESUME,
                wall=_wall(video_s, paused=paused),
                video_s=video_s + resume_off,
            )
        )
    video_s += 50.0
    rows.append(_event(EVENT_RECORD_STOP, wall=_wall(video_s, paused=paused), video_s=video_s))
    return rows


def _session(root: Path, *, filename: str = VIDEO) -> SessionRecord:
    record = SessionRecord(
        id=SESSION_ID,
        recording=RecordingInfo(
            path=f"recordings/{filename}",
            sha256="a" * 64,
            start_wallclock_vienna=T0.isoformat(),
            duration_s=100.0,
            filename=filename,
        ),
    )
    store.save_session(root, record)
    return record


def test_parse_includes_file_changed(tmp_path: Path) -> None:
    path = _write_jsonl(
        tmp_path / "clip.jsonl",
        [
            _event(EVENT_RECORD_START, wall=T0, video_s=0.0, file=VIDEO),
            {"event": EVENT_FILE_CHANGED, "file": "next.mkv", "wall": T0.isoformat()},
            _event(EVENT_RECORD_STOP, wall=_wall(10.0), video_s=10.0),
        ],
    )
    events = parse_sidecar_jsonl(path)
    assert [item.event for item in events] == [
        EVENT_RECORD_START,
        EVENT_FILE_CHANGED,
        EVENT_RECORD_STOP,
    ]
    assert events[1].file == "next.mkv"


def test_verified_sidecar_measured_offset() -> None:
    items = [_event_from_raw(row) for row in _verified_events()]
    duration = items[-1].video_s or 0.0
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=items,
        duration_s=duration,
        probe={"streams": [{"codec_type": "video", "start_time": "0.0"}]},
    )
    assert anchor.status == STATUS_VERIFIED
    assert anchor.method == METHOD_SIDECAR
    assert anchor.first_frame_offset_s == OFFSET
    assert anchor.first_frame_offset_source == SOURCE_MEASURED
    assert anchor.offset_source is None
    assert len(anchor.sections) == 2
    assert all(section.status == "verified" for section in anchor.sections)
    assert all(section.method == METHOD_SIDECAR for section in anchor.sections)
    assert abs((items[-1].wall - T0).total_seconds() - duration - OFFSET - 5.0) < 1e-9


def _nowall_events() -> list:
    return [
        _event_from_raw({"event": EVENT_RECORD_START, "video_s": 0.0, "file": VIDEO}),
        _event_from_raw({"event": EVENT_RECORD_STOP, "video_s": 100.0}),
    ]


def test_fallback_is_loud() -> None:
    events = _nowall_events()
    with pytest.warns(SidecarFallbackWarning, match="offset_source=fallback"):
        anchor = build_time_anchor(
            session_id=SESSION_ID,
            video_name=VIDEO,
            events=events,
            duration_s=100.0,
            probe={"streams": [{"codec_type": "video", "start_time": "0.0"}]},
        )
    assert anchor.first_frame_offset_s == FIRST_FRAME_OFFSET_EXPECTED_S
    assert anchor.first_frame_offset_source == SOURCE_FIXED
    assert anchor.offset_source == OFFSET_SOURCE_FALLBACK
    assert FALLBACK_WARNING in FALLBACK_WARNING


def test_fallback_warning_fires_every_clip() -> None:
    events = _nowall_events()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", SidecarFallbackWarning)
        build_time_anchor(
            session_id=SESSION_ID,
            video_name=VIDEO,
            events=events,
            duration_s=100.0,
        )
        build_time_anchor(
            session_id=SESSION_ID,
            video_name=VIDEO,
            events=events,
            duration_s=100.0,
        )
    fallbacks = [item for item in caught if issubclass(item.category, SidecarFallbackWarning)]
    assert len(fallbacks) == 2


def test_start_time_below_0_1_is_not_measured() -> None:
    items = [_event_from_raw(row) for row in _verified_events()]
    duration = items[-1].video_s or 0.0
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=items,
        duration_s=duration,
        probe={"streams": [{"codec_type": "video", "start_time": "0.05"}]},
    )
    assert anchor.first_frame_offset_source == SOURCE_MEASURED
    assert anchor.first_frame_offset_s == OFFSET
    assert anchor.offset_source is None


def test_first_frame_out_of_band() -> None:
    events = [_event_from_raw(row) for row in _verified_events()]
    duration = events[-1].video_s or 0.0
    for offset in (0.7, 1.4):
        anchor = build_time_anchor(
            session_id=SESSION_ID,
            video_name=VIDEO,
            events=events,
            duration_s=duration,
            measured_offset_s=offset,
        )
        assert anchor.status == STATUS_INVALID
        assert anchor.reason == REASON_FIRST_FRAME
        assert anchor.method is None
        assert anchor.first_frame_offset_s == offset


def test_n5_second_of_three_pauses_partial() -> None:
    """Resume outlier at the second of three pauses → sections 1–2 verified, 3–4 invalid."""
    for resume_bad in (0.2, 1.0):
        rows = _verified_events(resume_offsets=(0.58, resume_bad, 0.58))
        events = [_event_from_raw(row) for row in rows]
        duration = events[-1].video_s or 0.0
        resume2_wall = events[4].wall
        assert resume2_wall is not None
        fill_wall = resume2_wall + timedelta(seconds=10)
        anchor = build_time_anchor(
            session_id=SESSION_ID,
            video_name=VIDEO,
            events=events,
            duration_s=duration,
            measured_offset_s=OFFSET,
            fills=[SidecarFill(fill_id="F-sec3", wall=fill_wall)],
        )
        assert anchor.status == STATUS_PARTIAL
        assert [section.status for section in anchor.sections] == [
            "verified",
            "verified",
            "segment_invalid",
            "segment_invalid",
        ]
        assert [section.method for section in anchor.sections] == [
            METHOD_SIDECAR,
            METHOD_SIDECAR,
            None,
            None,
        ]
        assert anchor.sections[2].reason == "resume_offset_out_of_band"
        assert len(anchor.fills) == 1
        assert anchor.fills[0].video_s is None
        assert anchor.fills[0].reason == REASON_SEGMENT_INVALID
        assert anchor.fills[0].section == 3


def test_wrong_name_is_mismatch() -> None:
    rows = _verified_events()
    rows[0]["file"] = "other.mp4"
    events = [_event_from_raw(row) for row in rows]
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=events[-1].video_s,
        measured_offset_s=OFFSET,
    )
    assert anchor.status == STATUS_SIDECAR_MISMATCH
    assert anchor.method is None
    assert anchor.sections == []


def test_duration_plus_0_2_is_mismatch() -> None:
    events = [_event_from_raw(row) for row in _verified_events()]
    duration = (events[-1].video_s or 0.0) + 0.2
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=duration,
        measured_offset_s=OFFSET,
    )
    assert anchor.status == STATUS_SIDECAR_MISMATCH
    assert anchor.method is None


def test_corrupt_jsonl_is_invalid_not_no_sidecar(tmp_path: Path) -> None:
    path = tmp_path / "broken.jsonl"
    path.write_text("{not json\n", encoding="utf-8")
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        sidecar_path=path,
    )
    assert anchor.status == STATUS_INVALID
    assert anchor.reason == REASON_INVALID_JSON
    assert sidecar_replaces_detector(anchor) is False


def test_no_record_start_is_no_sidecar() -> None:
    events = [_event_from_raw(_event(EVENT_RECORD_STOP, wall=_wall(10.0), video_s=10.0))]
    anchor = build_time_anchor(session_id=SESSION_ID, video_name=VIDEO, events=events)
    assert anchor.status == STATUS_NO_SIDECAR


def test_no_record_stop_is_incomplete() -> None:
    events = [
        _event_from_raw(_event(EVENT_RECORD_START, wall=T0, video_s=0.0, file=VIDEO)),
        _event_from_raw(_event(EVENT_PAUSE, wall=_wall(10.0), video_s=10.0)),
    ]
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=10.0,
        measured_offset_s=OFFSET,
    )
    assert anchor.status == STATUS_INCOMPLETE
    assert anchor.method is None


def test_bit_identical_repeat(tva_root: Path) -> None:
    events = [_event_from_raw(row) for row in _verified_events()]
    duration = events[-1].video_s or 0.0
    first = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=duration,
        measured_offset_s=OFFSET,
    )
    path = write_time_anchor(tva_root, first)
    text1 = path.read_bytes()
    write_time_anchor(tva_root, first)
    assert path.read_bytes() == text1
    loaded = load_time_anchor(tva_root, SESSION_ID)
    assert loaded is not None
    assert loaded.as_dict() == first.as_dict()


def test_flag_off_does_not_write_time_anchor(
    tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_SIDECAR, raising=False)
    record = ingest(sample_video, root=tva_root)
    assert not time_anchor_path(tva_root, record.id).exists()
    assert write_session_time_anchor(record, root=tva_root) is None
    assert not time_anchor_path(tva_root, record.id).exists()


def test_flag_on_writes_time_anchor(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    record = _session(tva_root)
    sidecar = config.recordings_dir(tva_root) / VIDEO.replace(".mp4", ".jsonl")
    rows = _verified_events()
    _write_jsonl(sidecar, rows)
    duration = float(rows[-1]["video_s"])
    record = record.model_copy(
        update={
            "recording": record.recording.model_copy(update={"duration_s": duration})
        }
    )
    store.save_session(tva_root, record)
    anchor = write_session_time_anchor(
        record, root=tva_root, duration_s=duration, measured_offset_s=OFFSET
    )
    assert anchor is not None
    assert anchor.status == STATUS_VERIFIED
    path = time_anchor_path(tva_root, SESSION_ID)
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == "1"


def test_ensure_pause_check_ignores_leftover_ocr(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """TT:902: leftover OCR pause_checks must not replace a sidecar when the flag is on."""
    monkeypatch.setenv(ENV_SIDECAR, "1")
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    rows = _verified_events()
    duration = float(rows[-1]["video_s"])
    record = record.model_copy(
        update={
            "recording": record.recording.model_copy(update={"duration_s": duration})
        }
    )
    store.save_session(tva_root, record)
    _write_jsonl(config.recordings_dir(tva_root) / VIDEO.replace(".mp4", ".jsonl"), rows)
    write_session_time_anchor(
        record, root=tva_root, duration_s=duration, measured_offset_s=OFFSET
    )
    leftover = PauseCheck(
        session_id=record.id,
        recording_sha256=record.recording.sha256,
        part_shas=[],
        duration_s=duration,
        pause_check="suspected",
        pause_total_s=300.0,
        clock_resolution_s=1,
        pause_detectable_from_s=30.0,
        ocr_provider="paddleocr",
        ocr_model="ocr-v1",
    )
    write_pause_check(tva_root, leftover)
    check = ensure_pause_check(record, root=tva_root, apply_alignment=False)
    assert check.method == METHOD_SIDECAR
    assert check.pause_check == "clear"
    loaded = load_pause_check(tva_root, record.id)
    assert loaded is not None
    assert loaded.method == METHOD_SIDECAR
    assert loaded.pause_check == "clear"


def test_pause_guard_uses_sidecar_method(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    record = _session(tva_root)
    rows = _verified_events()
    duration = float(rows[-1]["video_s"])
    record = record.model_copy(
        update={
            "recording": record.recording.model_copy(update={"duration_s": duration})
        }
    )
    store.save_session(tva_root, record)
    _write_jsonl(config.recordings_dir(tva_root) / VIDEO.replace(".mp4", ".jsonl"), rows)
    write_session_time_anchor(
        record, root=tva_root, duration_s=duration, measured_offset_s=OFFSET
    )
    check = run_pause_guard(record, root=tva_root, apply_alignment=False)
    assert check.method == METHOD_SIDECAR
    assert check.pause_check == "clear"
    assert check.ocr_provider == METHOD_SIDECAR
    loaded = load_pause_check(tva_root, record.id)
    assert loaded is not None
    assert loaded.method == METHOD_SIDECAR


def test_pause_rule_media_continues_at_pause_video_s() -> None:
    events = [_event_from_raw(row) for row in _verified_events()]
    duration = events[-1].video_s or 0.0
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=duration,
        measured_offset_s=OFFSET,
    )
    first, second = anchor.sections
    assert first.end_event == EVENT_PAUSE
    assert second.start_event == EVENT_RESUME
    assert second.video_s_start == first.video_s_end
    assert second.resume_offset_s is not None
    assert 0.4 <= second.resume_offset_s <= 0.8


def test_schema_version_is_1() -> None:
    events = [_event_from_raw(row) for row in _verified_events()]
    anchor = build_time_anchor(
        session_id=SESSION_ID,
        video_name=VIDEO,
        events=events,
        duration_s=events[-1].video_s,
        measured_offset_s=OFFSET,
    )
    assert anchor.schema_version == "1"
    assert anchor.as_dict()["schema_version"] == "1"


@pytest.mark.skip(
    reason="[Rv] owner goldens not confirmed (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3)"
)
def test_rv_golden_test1_first_frame_offset() -> None:
    """Test 1 (2026-10-12): first-frame ≈ 1.043 s after record_start (TT:440, 902)."""
    raise AssertionError("pending owner confirmation")


@pytest.mark.skip(
    reason="[Rv] owner goldens not confirmed (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3)"
)
def test_rv_golden_test2_section_residual() -> None:
    """Test 2 (2026-10-12): each section ≤ 0.026 s; wall 594.685 vs video 593.667 (TT:439)."""
    raise AssertionError("pending owner confirmation")


@pytest.mark.skip(
    reason="[Rv] owner goldens not confirmed (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3)"
)
def test_rv_golden_session_171106_exit_t13() -> None:
    """Session 17:11:06: Exit T13 at video second ≈ 407–409 (TT:902)."""
    raise AssertionError("pending owner confirmation")


@pytest.mark.skip(
    reason="[Rv] owner goldens not confirmed (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3)"
)
def test_rv_golden_t_s2() -> None:
    """T-S2 sidecar golden — values not in the extract [NV]."""
    raise AssertionError("pending owner confirmation")


@pytest.mark.skip(
    reason="[Rv] owner goldens not confirmed (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3)"
)
def test_rv_golden_t_s3() -> None:
    """T-S3 sidecar golden — values not in the extract [NV]."""
    raise AssertionError("pending owner confirmation")
