from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import config, store
from tradevidanalyser.align import (
    FILENAME_CONFIDENCE,
    align_session,
    compute_alignment,
    confidence_from_residuals,
    theil_sen,
)
from tradevidanalyser.cli import main
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ocr import OcrRow, write_ocr_parquet
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.schema import Alignment, RecordingInfo, SessionRecord
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

VIENNA = timezone(timedelta(hours=2))
START = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)


def _session(
    root: Path,
    *,
    session_id: str = "2026-09-11_143000",
    start: datetime = START,
    duration_s: float = 7200.0,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename="2026-09-11 14-30-00.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _clock_row(video_t: float, *, offset_s: float, drift_s_per_h: float = 0.0) -> OcrRow:
    wall = START + timedelta(seconds=video_t + offset_s + drift_s_per_h * (video_t / 3600.0))
    return OcrRow(
        t=video_t,
        roi="clock",
        text=wall.strftime("%H:%M:%S"),
        confidence=0.95,
        parsed=wall.isoformat(),
    )


def _write_clocks(root: Path, session_id: str, rows: list[OcrRow]) -> None:
    extras = [
        OcrRow(t=0.0, roi="pnl", text="+12.5", confidence=0.9, parsed="12.5"),
    ]
    write_ocr_parquet(store.ocr_path(root, session_id), [*rows, *extras])


def test_theil_sen_recovers_known_offset_and_drift() -> None:
    offset = 1.8
    drift = 0.4
    xs = [float(i * 600) for i in range(12)]
    ys = [offset + drift * (t / 3600.0) for t in xs]
    slope, intercept = theil_sen(xs, ys)
    assert intercept == pytest.approx(offset, abs=1e-9)
    assert slope * 3600.0 == pytest.approx(drift, abs=1e-9)


def test_theil_sen_outlier_does_not_pull_fit() -> None:
    xs = [float(i * 300) for i in range(10)]
    ys = [1.8 for _ in xs]
    ys[5] = 61.8
    slope, intercept = theil_sen(xs, ys)
    assert intercept == pytest.approx(1.8, abs=1e-6)
    assert slope == pytest.approx(0.0, abs=1e-9)


def test_theil_sen_rejects_nonfinite() -> None:
    with pytest.raises(ValueError, match="finite"):
        theil_sen([0.0, math.nan], [1.0, 1.0])


def test_low_sample_count_low_confidence() -> None:
    assert confidence_from_residuals([0.0]) < 0.5
    assert confidence_from_residuals([0.0, 0.0]) < 0.5
    assert confidence_from_residuals([0.0] * 10) >= 0.9


def test_confidence_penalizes_systematic_bias() -> None:
    assert confidence_from_residuals([60.0] * 10) < 0.8
    assert confidence_from_residuals([0.0] * 9 + [60.0]) >= 0.9
    assert confidence_from_residuals([math.nan]) == 0.0


def test_ocr_clock_recovers_offset_and_drift(tva_root: Path) -> None:
    record = _session(tva_root)
    offset = 1.8
    drift = 0.4
    rows = [_clock_row(float(i * 600), offset_s=offset, drift_s_per_h=drift) for i in range(12)]
    _write_clocks(tva_root, record.id, rows)
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "ocr_clock"
    assert alignment.offset_s == pytest.approx(offset, abs=1e-6)
    assert alignment.drift_s_per_h == pytest.approx(drift, abs=1e-6)
    assert alignment.confidence >= 0.9
    assert len(alignment.samples) == 12
    within = sum(1 for sample in alignment.samples if abs(sample.residual_s) <= 2.0)
    assert within / len(alignment.samples) >= 0.9
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment == alignment
    assert store.compute_status(tva_root, record.id).stages["align"] == "ok"


def test_outlier_clock_rejected_from_fit(tva_root: Path) -> None:
    record = _session(tva_root)
    rows = [_clock_row(float(i * 300), offset_s=1.8) for i in range(10)]
    bad = START + timedelta(seconds=1500 + 1.8 + 60)
    rows[5] = OcrRow(
        t=1500.0,
        roi="clock",
        text=bad.strftime("%H:%M:%S"),
        confidence=0.95,
        parsed=bad.isoformat(),
    )
    _write_clocks(tva_root, record.id, rows)
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "ocr_clock"
    assert alignment.offset_s == pytest.approx(1.8, abs=0.05)
    assert alignment.drift_s_per_h == pytest.approx(0.0, abs=0.05)
    assert any(abs(sample.residual_s) > 50 for sample in alignment.samples)


def test_low_sample_count_session_low_confidence(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_clocks(tva_root, record.id, [_clock_row(0.0, offset_s=1.8), _clock_row(60.0, offset_s=1.8)])
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "ocr_clock"
    assert alignment.confidence < 0.5
    assert len(alignment.samples) == 2


def test_filename_fallback_without_ocr(tva_root: Path) -> None:
    record = _session(tva_root)
    alignment = align_session(record.id, root=tva_root)
    assert alignment == Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=FILENAME_CONFIDENCE,
        method="filename",
        samples=[],
    )
    assert store.compute_status(tva_root, record.id).stages["align"] == "ok"


def test_unparsed_clock_rows_filename_fallback(tva_root: Path) -> None:
    record = _session(tva_root)
    write_ocr_parquet(
        store.ocr_path(tva_root, record.id),
        [OcrRow(t=5.0, roi="clock", text="n/a", confidence=0.2, parsed=None)],
    )
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "filename"
    assert alignment.confidence == FILENAME_CONFIDENCE
    assert alignment.samples == []


def test_manual_offset_recorded(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    rows = [_clock_row(float(i * 600), offset_s=1.8) for i in range(8)]
    _write_clocks(tva_root, record.id, rows)
    assert (
        main(
            [
                "--root",
                str(tva_root),
                "align",
                record.id,
                "--manual-offset",
                "3.5",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"method": "manual"' in out
    assert '"offset_s": 3.5' in out
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "manual"
    assert loaded.alignment.offset_s == 3.5
    assert loaded.alignment.drift_s_per_h == 0.0
    assert loaded.alignment.samples


def test_manual_offset_without_ocr(tva_root: Path) -> None:
    record = _session(tva_root)
    alignment = align_session(record.id, root=tva_root, manual_offset=-2.25)
    assert alignment.method == "manual"
    assert alignment.offset_s == -2.25
    assert alignment.drift_s_per_h == 0.0
    assert alignment.confidence == 1.0
    assert alignment.samples == []


def test_align_omitted_from_status_until_run(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    status = store.compute_status(tva_root, record.id)
    assert status.stages["ingest"] == "ok"
    assert status.stages["transcribe"] == "ok"
    assert status.stages["extract"] == "ok"
    assert "align" not in status.stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing
    failed = client.get("/sessions", params={"status": "failed"}).json()["sessions"]
    assert record.id not in failed


def test_stale_align_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="align", error="align exploded")
    status = store.compute_status(tva_root, record.id)
    assert "align" not in status.stages
    assert status.error is None
    client = TestClient(create_app(tva_root))
    failed = client.get("/sessions", params={"status": "failed"}).json()["sessions"]
    assert record.id not in failed
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_align_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "align", "../outside"]) == 1
    assert main(["--root", str(tva_root), "align", "foo/bar"]) == 1


def test_align_refuses_record_id_path_escape(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_clocks(tva_root, record.id, [_clock_row(0.0, offset_s=1.8)])
    payload = store.read_json(store.session_json_path(tva_root, record.id))
    payload["id"] = "../outside"
    store.write_json(store.session_json_path(tva_root, record.id), payload)
    with pytest.raises(ValueError, match="does not match directory"):
        align_session(record.id, root=tva_root)
    assert not (tva_root / "outside" / "session.json").exists()
    assert store.session_json_path(tva_root, record.id).is_file()
    with pytest.raises(ValueError, match="unsafe session id"):
        compute_alignment(record.model_copy(update={"id": "../outside"}), root=tva_root)


def test_align_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "align" not in ALLOWED_RUN_STAGES


def test_align_rerun_byte_identical(tva_root: Path) -> None:
    record = _session(tva_root)
    rows = [_clock_row(float(i * 600), offset_s=1.8, drift_s_per_h=0.4) for i in range(12)]
    _write_clocks(tva_root, record.id, rows)
    align_session(record.id, root=tva_root)
    first = store.session_json_path(tva_root, record.id).read_text(encoding="utf-8")
    align_session(record.id, root=tva_root)
    assert store.session_json_path(tva_root, record.id).read_text(encoding="utf-8") == first


def test_invalidate_downstream_clears_alignment(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_clocks(tva_root, record.id, [_clock_row(0.0, offset_s=1.8)] * 1)
    align_session(record.id, root=tva_root)
    assert store.load_session(tva_root, record.id).alignment is not None
    store.invalidate_downstream(tva_root, record.id)
    assert store.load_session(tva_root, record.id).alignment is None
    assert "align" not in store.compute_status(tva_root, record.id).stages


def test_desk_exit_synthetic_residuals_and_confidence(tva_root: Path) -> None:
    """Stand-in for the desk exit: residuals ≤ 2 s at ≥ 90 % and confidence ≥ 0.9."""
    record = _session(tva_root)
    rows = [_clock_row(float(i * 480), offset_s=1.8, drift_s_per_h=0.05) for i in range(12)]
    _write_clocks(tva_root, record.id, rows)
    alignment = align_session(record.id, root=tva_root)
    assert alignment.confidence >= 0.9
    within = sum(1 for sample in alignment.samples if abs(sample.residual_s) <= 2.0)
    assert within / len(alignment.samples) >= 0.9


def test_naive_start_still_fits_aware_clocks(tva_root: Path) -> None:
    record = _session(tva_root)
    payload = store.read_json(store.session_json_path(tva_root, record.id))
    payload["recording"]["start_wallclock_vienna"] = START.replace(tzinfo=None).isoformat()
    store.write_json(store.session_json_path(tva_root, record.id), payload)
    rows = [_clock_row(float(i * 600), offset_s=1.8) for i in range(8)]
    _write_clocks(tva_root, record.id, rows)
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "ocr_clock"
    assert alignment.offset_s == pytest.approx(1.8, abs=1e-6)


def test_naive_ocr_clocks_still_fit(tva_root: Path) -> None:
    record = _session(tva_root)
    rows = []
    for i in range(8):
        video_t = float(i * 600)
        wall = START + timedelta(seconds=video_t + 1.8)
        rows.append(
            OcrRow(
                t=video_t,
                roi="clock",
                text=wall.strftime("%H:%M:%S"),
                confidence=0.95,
                parsed=wall.replace(tzinfo=None).isoformat(),
            )
        )
    _write_clocks(tva_root, record.id, rows)
    alignment = align_session(record.id, root=tva_root)
    assert alignment.method == "ocr_clock"
    assert alignment.offset_s == pytest.approx(1.8, abs=1e-6)
    assert len(alignment.samples) == 8


def test_unparseable_start_fail_closed(tva_root: Path) -> None:
    record = _session(tva_root)
    payload = store.read_json(store.session_json_path(tva_root, record.id))
    payload["recording"]["start_wallclock_vienna"] = "not-a-timestamp"
    store.write_json(store.session_json_path(tva_root, record.id), payload)
    with pytest.raises(ValueError, match="unparseable start_wallclock_vienna"):
        align_session(record.id, root=tva_root)
    assert store.load_session(tva_root, record.id).alignment is None


def test_nonfinite_manual_offset_rejected(tva_root: Path) -> None:
    record = _session(tva_root)
    with pytest.raises(ValueError, match="finite"):
        align_session(record.id, root=tva_root, manual_offset=math.nan)
    with pytest.raises(ValueError, match="finite"):
        align_session(record.id, root=tva_root, manual_offset=math.inf)
    assert store.load_session(tva_root, record.id).alignment is None
    assert main(["--root", str(tva_root), "align", record.id, "--manual-offset", "nan"]) == 1


def test_manual_offset_bias_lowers_confidence(tva_root: Path) -> None:
    record = _session(tva_root)
    rows = [_clock_row(float(i * 600), offset_s=1.8) for i in range(10)]
    _write_clocks(tva_root, record.id, rows)
    alignment = compute_alignment(store.load_session(tva_root, record.id), root=tva_root, manual_offset=61.8)
    assert alignment.method == "manual"
    assert alignment.offset_s == pytest.approx(61.8)
    assert alignment.confidence < 0.8


def test_invalidate_clears_alignment_when_record_id_differs(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_clocks(tva_root, record.id, [_clock_row(0.0, offset_s=1.8)])
    align_session(record.id, root=tva_root)
    payload = store.read_json(store.session_json_path(tva_root, record.id))
    payload["id"] = "other-id"
    store.write_json(store.session_json_path(tva_root, record.id), payload)
    store.invalidate_downstream(tva_root, record.id)
    cleared = store.read_json(store.session_json_path(tva_root, record.id))
    assert cleared["id"] == record.id
    assert cleared["alignment"] is None
    assert not store.session_json_path(tva_root, "other-id").exists()


@pytest.mark.golden
def test_golden_alignment_if_ocr_present() -> None:
    root = config.resolve_root()
    if not config.golden_dir(root).is_dir():
        pytest.skip("golden excerpt absent")
    for session_id in store.list_session_ids(root):
        if not store.ocr_path(root, session_id).is_file():
            continue
        alignment = align_session(session_id, root=root)
        if alignment.method != "ocr_clock" or not alignment.samples:
            continue
        within = sum(1 for sample in alignment.samples if abs(sample.residual_s) <= 2.0)
        assert within / len(alignment.samples) >= 0.9
        assert alignment.confidence >= 0.9
        return
    pytest.skip("no golden session with OCR clock rows")
