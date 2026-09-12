from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.frames import extract_frames
from tradevidanalyser.ingest import ingest
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.ocr import (
    FakeOcrProvider,
    MIN_PARSE_CONFIDENCE,
    OcrRow,
    get_ocr_provider,
    paddleocr_importable,
    parse_clock,
    parse_pnl,
    parse_position,
    parse_roi,
    read_ocr_parquet,
    write_ocr_parquet,
)
from tradevidanalyser.pipeline import ocr_session
from tradevidanalyser.schema import Chapter, RecordingInfo, SessionRecord


CLOCK_TEXT = "09:31:05"
PNL_TEXT = "-42.50"
POSITION_TEXT = "2"
INSTRUMENT_TEXT = "NQ"

TEST_LAYOUT = """
schema_version: "1"
layouts:
  quantower_default:
    description: "synthetic drawtext ROIs"
    rois:
      clock:        {x: 0.02, y: 0.05, w: 0.46, h: 0.22}
      position:     {x: 0.52, y: 0.05, w: 0.46, h: 0.22}
      pnl:          {x: 0.02, y: 0.55, w: 0.46, h: 0.22}
      instrument:   {x: 0.52, y: 0.55, w: 0.46, h: 0.22}
      account_mask: {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
      balance_mask: {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
"""


def _fontfile() -> str:
    for path in (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/freefont/FreeSans.ttf"),
    ):
        if path.is_file():
            return f"fontfile={path.as_posix()}:"
    return ""


def _draw(text: str, x: int, y: int) -> str:
    safe = text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return (
        f"drawtext={_fontfile()}text='{safe}':x={x}:y={y}:fontsize=32:"
        "fontcolor=white:box=1:boxcolor=black@0.7"
    )


def make_drawtext_frame(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = ",".join(
        [
            _draw(CLOCK_TEXT, 24, 28),
            _draw(POSITION_TEXT, 360, 28),
            _draw(PNL_TEXT, 24, 220),
            _draw(INSTRUMENT_TEXT, 360, 220),
        ]
    )
    result = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x203040:s=640x360:d=0.1",
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return dest


def _session(root: Path) -> SessionRecord:
    record = SessionRecord(
        id="2026-09-11_143000",
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=10.0,
            chapters=[Chapter(t=5.0, name="hotkey")],
            filename="2026-09-11 14-30-00.mp4",
        ),
    )
    store.save_session(root, record)
    return record


def _prepare_labeled_session(root: Path, *, confidence: float = 0.99) -> SessionRecord:
    (root / "layout.yaml").write_text(TEST_LAYOUT.strip() + "\n", encoding="utf-8")
    record = _session(root)
    frame = store.frame_path(root, record.id, 5.0)
    make_drawtext_frame(frame)
    sidecar = frame.with_name(frame.stem + ".ocr.json")
    sidecar.write_text(
        json.dumps(
            {
                "clock": {"text": CLOCK_TEXT, "confidence": confidence},
                "position": {"text": POSITION_TEXT, "confidence": confidence},
                "pnl": {"text": PNL_TEXT, "confidence": confidence},
                "instrument": {"text": INSTRUMENT_TEXT, "confidence": confidence},
            }
        ),
        encoding="utf-8",
    )
    store.compute_status(root, record.id)
    return record


def test_parse_known_drawtext_strings() -> None:
    prior = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)
    assert parse_clock(CLOCK_TEXT, prior=prior) == "2026-09-11T09:31:05+02:00"
    assert parse_pnl(PNL_TEXT) == "-42.5"
    assert parse_pnl("+150.25") == "150.25"
    assert parse_pnl("1.234,56") == "1234.56"
    assert parse_position(POSITION_TEXT) == "2"
    assert parse_position("-1") == "-1"


def test_low_confidence_keeps_text_and_nulls_parsed() -> None:
    prior = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)
    assert parse_roi("clock", CLOCK_TEXT, prior=prior, confidence=0.99) == "2026-09-11T09:31:05+02:00"
    assert parse_roi("clock", CLOCK_TEXT, prior=prior, confidence=0.2) is None
    assert 0.2 < MIN_PARSE_CONFIDENCE


def test_fake_is_default_provider() -> None:
    provider = get_ocr_provider()
    assert isinstance(provider, FakeOcrProvider)
    assert provider.name == "fake"


def test_drawtext_frame_ocr_parses(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TVA_LAYOUT", str(tva_root / "layout.yaml"))
    record = _prepare_labeled_session(tva_root)
    assert store.frame_path(tva_root, record.id, 5.0).is_file()
    payload = ocr_session(record.id, root=tva_root, provider_name="fake")
    assert payload["provider"] == "fake"
    assert payload["rows"] == 4
    rows = {row.roi: row for row in read_ocr_parquet(store.ocr_path(tva_root, record.id))}
    assert rows["clock"].text == CLOCK_TEXT
    assert rows["clock"].parsed == "2026-09-11T09:31:05+02:00"
    assert rows["pnl"].text == PNL_TEXT
    assert rows["pnl"].parsed == "-42.5"
    assert rows["position"].text == POSITION_TEXT
    assert rows["position"].parsed == "2"
    assert rows["instrument"].text == INSTRUMENT_TEXT
    assert rows["instrument"].parsed is None
    assert store.compute_status(tva_root, record.id).stages["ocr"] == "ok"


def test_low_confidence_row_kept_parsed_null(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TVA_LAYOUT", str(tva_root / "layout.yaml"))
    record = _prepare_labeled_session(tva_root, confidence=0.1)
    ocr_session(record.id, root=tva_root)
    rows = read_ocr_parquet(store.ocr_path(tva_root, record.id))
    assert rows
    assert all(row.text for row in rows)
    assert all(row.parsed is None for row in rows)


def test_fake_round_trip_byte_identical(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TVA_LAYOUT", str(tva_root / "layout.yaml"))
    record = _prepare_labeled_session(tva_root)
    first = ocr_session(record.id, root=tva_root)
    raw = store.ocr_path(tva_root, record.id).read_bytes()
    second = ocr_session(record.id, root=tva_root)
    assert first["rows"] == second["rows"]
    assert store.ocr_path(tva_root, record.id).read_bytes() == raw


def test_cli_ocr_and_status_omits_missing(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("TVA_LAYOUT", str(tva_root / "layout.yaml"))
    record = _prepare_labeled_session(tva_root)
    assert "ocr" not in store.compute_status(tva_root, record.id).stages
    assert main(["--root", str(tva_root), "ocr", record.id]) == 0
    out = capsys.readouterr().out
    assert '"provider": "fake"' in out
    assert '"rows": 4' in out
    assert store.compute_status(tva_root, record.id).stages["ocr"] == "ok"


def test_ocr_missing_does_not_trip_status_filter(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    from tradevidanalyser.pipeline import extract_session, transcribe_session

    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    status = store.compute_status(tva_root, record.id)
    assert status.stages["ingest"] == "ok"
    assert status.stages["transcribe"] == "ok"
    assert status.stages["extract"] == "ok"
    assert "ocr" not in status.stages
    assert "frames" not in status.stages


def test_ocr_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "ocr", "../outside"]) == 1
    assert main(["--root", str(tva_root), "ocr", "foo/bar"]) == 1


def test_ocr_requires_frames(tva_root: Path) -> None:
    record = _session(tva_root)
    with pytest.raises(Exception, match="no frames"):
        ocr_session(record.id, root=tva_root)


def test_doctor_paddleocr_is_warn_when_fake(tva_root: Path) -> None:
    ids = {c.id: c for c in run_doctor(tva_root).checks}
    assert ids["paddleocr"].status in {"ok", "warn"}


def test_write_read_parquet_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ocr.parquet"
    rows = [
        OcrRow(t=5.0, roi="clock", text=CLOCK_TEXT, confidence=0.9, parsed="2026-09-11T09:31:05+02:00"),
        OcrRow(t=5.0, roi="pnl", text=PNL_TEXT, confidence=0.1, parsed=None),
    ]
    write_ocr_parquet(path, rows)
    back = read_ocr_parquet(path)
    assert back == rows


@pytest.mark.golden
def test_golden_ocr_clock_within_1s_of_filename_prior(tmp_path: Path) -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    media_files = sorted(golden.glob("*.mp4")) + sorted(golden.glob("*.mkv"))
    if not media_files:
        pytest.skip("golden media absent")
    if not paddleocr_importable():
        pytest.skip("paddleocr not installed")

    root = tmp_path / "store"
    config.ensure_layout(root)
    record = ingest(media_files[0], root=root)
    duration = float(record.recording.duration_s)
    times = [t for t in (0.0, 30.0, 60.0, 120.0, 180.0, 300.0) if t <= duration]
    if record.recording.chapters:
        from tradevidanalyser.frames import default_frame_times

        times = default_frame_times(record) or times
    if not times:
        pytest.skip("golden recording too short for clock samples")
    extract_frames(record, times[:12], root=root)
    ocr_session(record.id, root=root, provider_name="paddleocr")
    rows = [
        row
        for row in read_ocr_parquet(store.ocr_path(root, record.id))
        if row.roi == "clock" and row.parsed
    ]
    if not rows:
        pytest.skip("no parsed clock rows on golden excerpt")
    prior = datetime.fromisoformat(record.recording.start_wallclock_vienna)
    hits = 0
    for row in rows:
        parsed = datetime.fromisoformat(row.parsed)
        expected = prior + timedelta(seconds=row.t)
        if abs((parsed - expected).total_seconds()) <= 1.0:
            hits += 1
    assert hits / len(rows) >= 0.90
