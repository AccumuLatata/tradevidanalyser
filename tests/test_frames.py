from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.frames import (
    REQUIRED_ROIS,
    accurate_seek_cmd,
    default_frame_times,
    extract_frames,
    get_layout,
    load_layouts,
    media_for_time,
)
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import run_latest
from tradevidanalyser.schema import Chapter, RecordingInfo, RecordingPart, SessionRecord
from tradevidanalyser.serve import create_app

# MP4/QuickTime chapter tracks rewrite a first chapter that does not start at
# t=0. Tests that need a mid-tape marker write it onto session.json instead.


def _session(
    *,
    duration_s: float,
    chapters: list[tuple[float, str]],
    path: str = "recordings/x.mp4",
    parts: list | None = None,
) -> SessionRecord:
    return SessionRecord(
        id="2026-09-11_143000",
        recording=RecordingInfo(
            path=path,
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=duration_s,
            chapters=[Chapter(t=t, name=name) for t, name in chapters],
            filename=Path(path).name,
            parts=parts or [],
        ),
    )


def test_layout_loads_required_rois() -> None:
    layouts = load_layouts()
    assert "quantower_default" in layouts
    layout = get_layout("quantower_default")
    assert set(layout.rois) >= set(REQUIRED_ROIS)
    clock = layout.rois["clock"]
    assert 0.0 <= clock.x <= 1.0
    assert clock.w > 0


def test_layout_rejects_out_of_range_roi(tmp_path: Path) -> None:
    bad = tmp_path / "layout.yaml"
    bad.write_text(
        "\n".join(
            [
                'schema_version: "1"',
                "layouts:",
                "  bad:",
                "    rois:",
                "      clock:        {x: 1.2, y: 0.0, w: 0.1, h: 0.1}",
                "      position:     {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      pnl:          {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      instrument:   {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      account_mask: {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      balance_mask: {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="outside 0..1"):
        load_layouts(bad)


def test_layout_rejects_roi_that_extends_past_the_frame(tmp_path: Path) -> None:
    bad = tmp_path / "layout.yaml"
    bad.write_text(
        "\n".join(
            [
                'schema_version: "1"',
                "layouts:",
                "  bad:",
                "    rois:",
                "      clock:        {x: 0.90, y: 0.0, w: 0.20, h: 0.1}",
                "      position:     {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      pnl:          {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      instrument:   {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      account_mask: {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
                "      balance_mask: {x: 0.0, y: 0.0, w: 0.0, h: 0.0}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="x\\+w"):
        load_layouts(bad)


def test_default_times_are_chapter_plus_minus_0_2_5() -> None:
    session = _session(duration_s=10.0, chapters=[(5.0, "hotkey")])
    assert default_frame_times(session) == [0.0, 3.0, 5.0, 7.0, 10.0]


def test_default_times_drop_out_of_range() -> None:
    session = _session(duration_s=8.0, chapters=[(1.0, "early")])
    assert default_frame_times(session) == [1.0, 3.0, 6.0]


def test_accurate_seek_places_ss_after_input(tmp_path: Path) -> None:
    src = tmp_path / "in.mp4"
    dest = tmp_path / "out.jpg"
    cmd = accurate_seek_cmd("ffmpeg", src, 3.25, dest)
    assert cmd.index("-i") < cmd.index("-ss")
    assert cmd[cmd.index("-ss") + 1] == "3.250"
    assert cmd[cmd.index("-frames:v") + 1] == "1"


def test_extract_frames_count_and_contact_sheet(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=10.2,
        chapters=[(5.0, "hotkey")],
        drawtext_pts=True,
        size="320x180",
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=5.0, name="hotkey")]
    store.save_session(tva_root, record)
    record = store.load_session(tva_root, record.id)
    result = extract_frames(record, root=tva_root, contact_sheet=True)
    assert result.times == [0.0, 3.0, 5.0, 7.0, 10.0]
    assert len(result.frames) == 5
    for path, t in zip(result.frames, result.times, strict=True):
        assert path.is_file()
        assert path.stat().st_size > 0
        assert path.name == store.frame_filename(t)
        assert path.parent == store.frames_dir(tva_root, record.id)
    assert result.contact_sheet is not None
    assert result.contact_sheet.is_file()
    assert result.contact_sheet.name == "contact_sheet.jpg"
    status = store.compute_status(tva_root, record.id)
    assert status.stages["frames"] == "ok"


def test_cli_frames_at_and_contact_sheet(
    tva_root: Path, tmp_path: Path, write_video, capsys
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=8.0,
        chapters=[(4.0, "mark")],
        drawtext_pts=True,
        size="320x180",
    )
    record = ingest(video, root=tva_root)
    assert "frames" not in store.compute_status(tva_root, record.id).stages
    code = main(
        [
            "--root",
            str(tva_root),
            "frames",
            record.id,
            "--at",
            "1.5",
            "4.0",
            "--contact-sheet",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert '"count": 2' in out
    assert "1.500.jpg" in out
    assert "4.000.jpg" in out
    assert "contact_sheet.jpg" in out
    assert store.frame_path(tva_root, record.id, 1.5).is_file()
    assert store.contact_sheet_path(tva_root, record.id).is_file()
    assert store.compute_status(tva_root, record.id).stages["frames"] == "ok"


def test_cli_frames_uses_chapter_defaults(
    tva_root: Path, tmp_path: Path, write_video, capsys
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=10.2,
        chapters=[(5.0, "hotkey")],
        drawtext_pts=True,
        size="320x180",
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=5.0, name="hotkey")]
    store.save_session(tva_root, record)
    record = store.load_session(tva_root, record.id)
    assert default_frame_times(record) == [0.0, 3.0, 5.0, 7.0, 10.0]
    assert main(["--root", str(tva_root), "frames", record.id]) == 0
    payload = capsys.readouterr().out
    assert '"count": 5' in payload
    names = {path.name for path in store.list_frame_jpgs(tva_root, record.id)}
    assert names == {"0.000.jpg", "3.000.jpg", "5.000.jpg", "7.000.jpg", "10.000.jpg"}


def test_split_session_seeks_inside_the_owning_part(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=3.0, drawtext_pts=True, size="320x180")
    write_video(
        tmp_path / "2026-09-11 14-30-03.mp4",
        seconds=3.0,
        chapters=[(1.0, "second")],
        drawtext_pts=True,
        size="320x180",
    )
    record = ingest(tmp_path / "2026-09-11 14-30-00.mp4", root=tva_root)
    assert len(record.recording.parts) == 2
    chapter_t = next(ch.t for ch in record.recording.chapters if ch.name == "second")
    src, seek = media_for_time(record, tva_root, chapter_t)
    assert src.name == "2026-09-11 14-30-03.mp4"
    assert seek == pytest.approx(chapter_t - record.recording.parts[1].offset_s, abs=0.05)
    result = extract_frames(record, [chapter_t], root=tva_root)
    assert len(result.frames) == 1
    assert result.frames[0].is_file()


def test_media_for_time_uses_first_part_or_gap_end_not_last(
    tva_root: Path,
) -> None:
    session = _session(
        duration_s=10.0,
        chapters=[],
        parts=[
            RecordingPart(
                path="recordings/a.mp4",
                sha256="0" * 64,
                duration_s=3.0,
                offset_s=2.0,
                filename="a.mp4",
            ),
            RecordingPart(
                path="recordings/b.mp4",
                sha256="1" * 64,
                duration_s=3.0,
                offset_s=6.0,
                filename="b.mp4",
            ),
        ],
    )
    before_src, before_seek = media_for_time(session, tva_root, 0.5)
    assert before_src.name == "a.mp4"
    assert before_seek == 0.0
    gap_src, gap_seek = media_for_time(session, tva_root, 5.5)
    assert gap_src.name == "a.mp4"
    assert gap_seek == pytest.approx(2.96, abs=0.02)


def test_extract_frames_drops_times_past_duration(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4", seconds=4.0, drawtext_pts=True, size="320x180"
    )
    record = ingest(video, root=tva_root)
    result = extract_frames(record, [1.0, 99.0], root=tva_root)
    assert result.times == [1.0]
    assert len(result.frames) == 1
    with pytest.raises(ValueError, match="outside session duration"):
        extract_frames(record, [99.0], root=tva_root)


def test_list_frame_jpgs_sorts_numerically(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4", seconds=11.0, drawtext_pts=True, size="320x180"
    )
    record = ingest(video, root=tva_root)
    extract_frames(record, [2.0, 10.0], root=tva_root)
    names = [path.name for path in store.list_frame_jpgs(tva_root, record.id)]
    assert names == ["2.000.jpg", "10.000.jpg"]


def test_contact_sheet_incomplete_grid(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4", seconds=8.0, drawtext_pts=True, size="320x180"
    )
    record = ingest(video, root=tva_root)
    result = extract_frames(
        record, [0.0, 1.0, 2.0, 3.0, 4.0, 5.0], root=tva_root, contact_sheet=True
    )
    assert len(result.frames) == 6
    assert result.contact_sheet is not None
    assert result.contact_sheet.is_file()
    assert result.contact_sheet.stat().st_size > 0


def test_changed_recording_drops_stale_frames(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    part1 = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0, drawtext_pts=True, size="320x180"
    )
    record = ingest(part1, root=tva_root)
    extract_frames(record, [0.5], root=tva_root)
    assert store.list_frame_jpgs(tva_root, record.id)
    assert store.compute_status(tva_root, record.id).stages["frames"] == "ok"
    write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0, size="320x180")
    updated = ingest(part1, root=tva_root)
    assert len(updated.recording.parts) == 2
    assert store.list_frame_jpgs(tva_root, record.id) == []
    assert "frames" not in store.compute_status(tva_root, record.id).stages


def test_frames_refuse_path_escape_session_id(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "frames", "../outside"]) == 1
    assert main(["--root", str(tva_root), "frames", "foo/bar"]) == 1
    assert not (tva_root.parent / "outside" / "frames").exists()


def test_frames_missing_does_not_trip_status_filter(
    tva_root: Path, sample_video: Path
) -> None:
    record = run_latest(tva_root, video=sample_video)
    status = store.compute_status(tva_root, record.id)
    assert status.stages["ingest"] == "ok"
    assert status.stages["transcribe"] == "ok"
    assert status.stages["extract"] == "ok"
    assert "frames" not in status.stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_accurate_seek_lands_on_the_requested_color(
    tva_root: Path, tmp_path: Path
) -> None:
    video = tmp_path / "2026-09-11 14-30-00.mp4"
    _write_color_switch_video(video, red_s=3.0, blue_s=3.0)
    record = ingest(video, root=tva_root)
    result = extract_frames(record, [1.0, 5.0], root=tva_root)
    red = _center_rgb(result.frames[0])
    blue = _center_rgb(result.frames[1])
    assert red[0] > red[1] and red[0] > red[2]
    assert blue[2] > blue[0] and blue[2] > blue[1]


def _write_color_switch_video(dest: Path, *, red_s: float, blue_s: float) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = red_s + blue_s
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=red:s=160x120:d={red_s}",
        "-f",
        "lavfi",
        "-i",
        f"color=c=blue:s=160x120:d={blue_s}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={total}",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-map",
        "2:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "250",
        "-c:a",
        "aac",
        str(dest),
    ]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return dest


def _center_rgb(path: Path) -> tuple[int, int, int]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "rgb24",
            "pipe:1",
        ],
        check=False,
        capture_output=True,
    )
    if result.returncode != 0 or len(result.stdout) < 3:
        raise RuntimeError(result.stderr.decode("utf-8", errors="replace"))
    return result.stdout[0], result.stdout[1], result.stdout[2]
