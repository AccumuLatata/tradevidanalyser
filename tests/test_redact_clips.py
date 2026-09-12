from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tradevidanalyser import media, store
from tradevidanalyser.cli import main
from tradevidanalyser.clips import _ffmpeg_cut, clip_window, extract_clips
from tradevidanalyser.frames import Roi, extract_frames, get_layout
from tradevidanalyser.ingest import ingest
from tradevidanalyser.media import MediaError
from tradevidanalyser.pipeline import clips_session, run_latest
from tradevidanalyser.redact import drawbox_filter, mask_rois
from tradevidanalyser.schema import Chapter, RecordingInfo, RecordingPart, SessionRecord
from tradevidanalyser.serve import create_app

from fastapi.testclient import TestClient

MASK_LAYOUT = """
schema_version: "1"
layouts:
  quantower_default:
    description: "synthetic masks for pixel tests"
    rois:
      clock:        {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
      position:     {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
      pnl:          {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
      instrument:   {x: 0.00, y: 0.00, w: 0.00, h: 0.00}
      account_mask: {x: 0.00, y: 0.00, w: 0.35, h: 0.35}
      balance_mask: {x: 0.65, y: 0.65, w: 0.35, h: 0.35}
"""


def _write_solid_video(
    dest: Path,
    *,
    seconds: float = 12.0,
    color: str = "red",
    extra_audio: bool = False,
    chapters: list[tuple[float, str]] | None = None,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest if not chapters else dest.with_name(dest.stem + ".__raw__.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c={color}:s=320x240:d={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
    ]
    if extra_audio:
        cmd.extend(["-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}"])
    cmd.extend(["-map", "0:v:0", "-map", "1:a:0"])
    if extra_audio:
        cmd.extend(["-map", "2:a:0"])
    cmd.extend(
        [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "1",
            "-keyint_min",
            "1",
            "-c:a",
            "aac",
            "-metadata:s:a:0",
            "title=mic",
        ]
    )
    if extra_audio:
        cmd.extend(["-metadata:s:a:1", "title=desktop"])
    cmd.append(str(raw))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    if not chapters:
        return dest
    meta = dest.with_name(dest.stem + ".__ffmeta__.txt")
    lines = [";FFMETADATA1"]
    duration_ms = max(int(seconds * 1000), 1)
    for start_s, title in chapters:
        start_ms = max(int(start_s * 1000), 0)
        end_ms = min(start_ms + 500, duration_ms)
        if end_ms <= start_ms:
            end_ms = start_ms + 1
        safe = title.replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;")
        lines.extend(
            [
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start_ms}",
                f"END={end_ms}",
                f"title={safe}",
            ]
        )
    meta.write_text("\n".join(lines) + "\n", encoding="utf-8")
    remux = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(raw),
            "-i",
            str(meta),
            "-map",
            "0",
            "-map_metadata",
            "1",
            "-c",
            "copy",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    raw.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    if remux.returncode != 0:
        raise RuntimeError(remux.stderr)
    return dest


def _region_mean_rgb(path: Path, roi: Roi) -> tuple[float, float, float]:
    result = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-vf",
            f"crop=iw*{roi.w}:ih*{roi.h}:iw*{roi.x}:ih*{roi.y},scale=1:1",
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


def _center_rgb(path: Path) -> tuple[int, int, int]:
    return _region_mean_rgb(path, Roi(x=0.45, y=0.45, w=0.10, h=0.10))


def _audio_count(path: Path) -> int:
    probe = media.ffprobe(path)
    return sum(1 for stream in probe.get("streams") or [] if stream.get("codec_type") == "audio")


def _install_mask_layout(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = root / "layout.yaml"
    path.write_text(MASK_LAYOUT.strip() + "\n", encoding="utf-8")
    monkeypatch.setenv("TVA_LAYOUT", str(path))


def test_clip_window_clamps_to_session() -> None:
    assert clip_window(5.0, 12.0) == (0.0, 12.0)
    assert clip_window(60.0, 200.0) == (30.0, 120.0)
    assert clip_window(0.0, 90.0) == (0.0, 60.0)


def test_mask_rois_skip_zero_area() -> None:
    layout = get_layout()
    assert mask_rois(layout) == {}
    assert drawbox_filter(layout) is None


def test_extracted_frame_mask_region_is_black(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mask_layout(tva_root, monkeypatch)
    video = _write_solid_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=8.0,
        chapters=[(4.0, "hotkey")],
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=4.0, name="hotkey")]
    store.save_session(tva_root, record)
    result = extract_frames(record, [4.0], root=tva_root)
    layout = get_layout(root=tva_root)
    account = _region_mean_rgb(result.frames[0], layout.rois["account_mask"])
    balance = _region_mean_rgb(result.frames[0], layout.rois["balance_mask"])
    center = _center_rgb(result.frames[0])
    assert max(account) < 20
    assert max(balance) < 20
    assert center[0] > 80 and center[0] > center[1] and center[0] > center[2]


def test_default_layout_does_not_black_the_frame(
    tva_root: Path, tmp_path: Path
) -> None:
    video = _write_solid_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=4.0)
    record = ingest(video, root=tva_root)
    result = extract_frames(record, [1.0], root=tva_root)
    center = _center_rgb(result.frames[0])
    assert center[0] > 80


def test_clip_duration_and_single_audio_track(
    tva_root: Path, tmp_path: Path
) -> None:
    video = _write_solid_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=12.0,
        extra_audio=True,
        chapters=[(4.0, "hotkey")],
    )
    record = ingest(video, root=tva_root, desktop_track=True)
    record.recording.chapters = [Chapter(t=4.0, name="hotkey")]
    store.save_session(tva_root, record)
    assert "desktop" in record.recording.tracks or store.desktop_audio_path(
        tva_root, record.id
    ).is_file()
    assert "clips" not in store.compute_status(tva_root, record.id).stages
    result = extract_clips(record, root=tva_root)
    assert len(result.clips) == 1
    clip = result.clips[0]
    assert clip.is_file()
    expected = clip_window(4.0, record.recording.duration_s)
    duration = media.duration_s(media.ffprobe(clip))
    assert duration == pytest.approx(expected[1] - expected[0], abs=1.0)
    assert _audio_count(clip) == 1
    assert store.compute_status(tva_root, record.id).stages["clips"] == "ok"


def test_cli_clips_redact_blacks_mask(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    _install_mask_layout(tva_root, monkeypatch)
    video = _write_solid_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=8.0,
        extra_audio=True,
        chapters=[(3.0, "mark")],
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=3.0, name="mark")]
    store.save_session(tva_root, record)
    assert main(["--root", str(tva_root), "clips", record.id, "--redact"]) == 0
    out = capsys.readouterr().out
    assert '"count": 1' in out
    assert '"redacted": true' in out
    clip = store.list_clips(tva_root, record.id)[0]
    # Grab one frame from the redacted clip and check the mask.
    frame = tmp_path / "from_clip.jpg"
    grabbed = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(clip),
            "-ss",
            "0.2",
            "-frames:v",
            "1",
            str(frame),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert grabbed.returncode == 0
    layout = get_layout(root=tva_root)
    assert max(_region_mean_rgb(frame, layout.rois["account_mask"])) < 20
    assert _audio_count(clip) == 1


def test_clips_missing_does_not_trip_status_filter(
    tva_root: Path, sample_video: Path
) -> None:
    record = run_latest(tva_root, video=sample_video)
    status = store.compute_status(tva_root, record.id)
    assert status.stages["ingest"] == "ok"
    assert "clips" not in status.stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"})
    assert record.id not in missing.json()["sessions"]
    failed = client.get("/sessions", params={"status": "failed"})
    assert record.id not in failed.json()["sessions"]


def test_stale_clips_failed_is_omitted_without_mp4(
    tva_root: Path, sample_video: Path
) -> None:
    record = run_latest(tva_root, video=sample_video)
    store.compute_status(tva_root, record.id, failed="clips", error="clips exploded")
    status = store.compute_status(tva_root, record.id)
    assert "clips" not in status.stages
    assert status.error is None
    client = TestClient(create_app(tva_root))
    failed = client.get("/sessions", params={"status": "failed"}).json()["sessions"]
    assert record.id not in failed
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_empty_clip_file_does_not_mark_clips_ok(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    folder = store.clips_dir(tva_root, record.id)
    folder.mkdir(parents=True, exist_ok=True)
    empty = folder / "1.000.mp4"
    empty.write_bytes(b"")
    assert store.list_clips(tva_root, record.id) == []
    assert "clips" not in store.compute_status(tva_root, record.id).stages


def test_failed_redact_does_not_leave_unredacted_frame(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_mask_layout(tva_root, monkeypatch)
    video = _write_solid_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=4.0)
    record = ingest(video, root=tva_root)
    dest = store.frame_path(tva_root, record.id, 1.0)
    real_run = subprocess.run

    def boom(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        if isinstance(cmd, list) and any(isinstance(part, str) and "drawbox" in part for part in cmd):
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="drawbox boom")
        return real_run(*args, **kwargs)

    monkeypatch.setattr("tradevidanalyser.redact.subprocess.run", boom)
    with pytest.raises(MediaError, match="drawbox boom"):
        extract_frames(record, [1.0], root=tva_root)
    assert not dest.exists()
    assert store.list_frame_jpgs(tva_root, record.id) == []
    assert "frames" not in store.compute_status(tva_root, record.id).stages


def test_failed_clip_cut_does_not_leave_dest(tva_root: Path, tmp_path: Path) -> None:
    video_only = tmp_path / "video-only.mp4"
    made = subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x120:d=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video_only),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert made.returncode == 0
    dest = store.clips_dir(tva_root, "2026-09-11_143000") / "1.000.mp4"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(MediaError):
        _ffmpeg_cut(video_only, 0.0, 1.0, dest)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".tmp").exists()


def test_clips_refuse_part_outside_root(tva_root: Path) -> None:
    session = SessionRecord(
        id="2026-09-11_143000",
        recording=RecordingInfo(
            path="recordings/inside.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=10.0,
            chapters=[Chapter(t=4.0, name="hotkey")],
            parts=[
                RecordingPart(
                    path="/tmp/outside_secret.mp4",
                    sha256="1" * 64,
                    duration_s=10.0,
                    offset_s=0.0,
                    filename="outside_secret.mp4",
                )
            ],
        ),
    )
    store.save_session(tva_root, session)
    with pytest.raises(ValueError, match="outside TVA_ROOT"):
        extract_clips(session, root=tva_root)
    assert store.list_clips(tva_root, session.id) == []


def test_clip_duration_uses_ss_to_input_window(
    tva_root: Path, tmp_path: Path
) -> None:
    video = _write_solid_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=12.0,
        chapters=[(8.0, "mid")],
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=8.0, name="mid")]
    store.save_session(tva_root, record)
    result = extract_clips(record, root=tva_root, before_s=2.0, after_s=3.0)
    assert len(result.clips) == 1
    expected = clip_window(8.0, record.recording.duration_s, before_s=2.0, after_s=3.0)
    assert expected == (6.0, 11.0)
    duration = media.duration_s(media.ffprobe(result.clips[0]))
    assert duration == pytest.approx(5.0, abs=1.0)


def test_split_session_cuts_inside_owning_parts(
    tva_root: Path, tmp_path: Path
) -> None:
    _write_solid_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=3.0)
    _write_solid_video(
        tmp_path / "2026-09-11 14-30-03.mp4",
        seconds=3.0,
        chapters=[(1.0, "second")],
    )
    record = ingest(tmp_path / "2026-09-11 14-30-00.mp4", root=tva_root)
    assert len(record.recording.parts) == 2
    chapter_t = next(ch.t for ch in record.recording.chapters if ch.name == "second")
    result = extract_clips(record, root=tva_root, before_s=0.4, after_s=0.4)
    assert len(result.clips) == 1
    assert result.times == [pytest.approx(chapter_t, abs=0.05)]
    duration = media.duration_s(media.ffprobe(result.clips[0]))
    assert duration == pytest.approx(0.8, abs=0.5)
    assert _audio_count(result.clips[0]) == 1
    stitch = record.recording.parts[1].offset_s
    record.recording.chapters = [Chapter(t=stitch, name="stitch")]
    store.save_session(tva_root, record)
    spanned = extract_clips(record, root=tva_root, before_s=1.0, after_s=1.0)
    assert len(spanned.clips) == 1
    assert media.duration_s(media.ffprobe(spanned.clips[0])) == pytest.approx(2.0, abs=0.6)
    assert _audio_count(spanned.clips[0]) == 1


def test_rerun_drops_stale_clip_names(
    tva_root: Path, tmp_path: Path
) -> None:
    video = _write_solid_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=8.0,
        chapters=[(2.0, "old")],
    )
    record = ingest(video, root=tva_root)
    record.recording.chapters = [Chapter(t=2.0, name="old")]
    store.save_session(tva_root, record)
    extract_clips(record, root=tva_root, before_s=0.5, after_s=0.5)
    stale = store.list_clips(tva_root, record.id)
    assert stale
    record.recording.chapters = [Chapter(t=6.0, name="new")]
    store.save_session(tva_root, record)
    extract_clips(record, root=tva_root, before_s=0.5, after_s=0.5)
    names = {path.name for path in store.list_clips(tva_root, record.id)}
    assert any(name.startswith("6.000") for name in names)
    assert not any(name.startswith("2.000") for name in names)


def test_clips_refuse_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "clips", "../outside"]) == 1
    assert main(["--root", str(tva_root), "clips", "foo/bar"]) == 1


def test_changed_recording_drops_stale_clips(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    first = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=4.0)
    record = ingest(first, root=tva_root)
    record.recording.chapters = [Chapter(t=1.0, name="a")]
    store.save_session(tva_root, record)
    clips_session(record.id, root=tva_root)
    assert store.list_clips(tva_root, record.id)
    second = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=6.0)
    ingest(second, root=tva_root)
    assert store.list_clips(tva_root, record.id) == []
    assert "clips" not in store.compute_status(tva_root, record.id).stages
