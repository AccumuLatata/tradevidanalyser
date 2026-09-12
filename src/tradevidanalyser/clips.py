"""Chapter clips: stream-copy, mic only. Optional ``--redact`` drawboxes."""

from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from tradevidanalyser import media, store
from tradevidanalyser.frames import Layout, get_layout, media_for_time, quantize_time
from tradevidanalyser.media import MediaError
from tradevidanalyser.redact import drawbox_filter
from tradevidanalyser.schema import SessionRecord

CLIP_BEFORE_S = 30.0
CLIP_AFTER_S = 60.0


@dataclass(frozen=True)
class ClipResult:
    session_id: str
    clips: list[Path]
    times: list[float]
    redacted: bool

    def as_dict(self, root: Path) -> dict:
        def rel(path: Path) -> str:
            try:
                return str(path.resolve().relative_to(root.resolve()))
            except ValueError:
                return str(path)

        return {
            "session_id": self.session_id,
            "clips": [rel(path) for path in self.clips],
            "times": self.times,
            "count": len(self.clips),
            "redacted": self.redacted,
        }


def clip_window(
    chapter_t: float,
    duration_s: float,
    *,
    before_s: float = CLIP_BEFORE_S,
    after_s: float = CLIP_AFTER_S,
) -> tuple[float, float]:
    start = max(0.0, float(chapter_t) - before_s)
    end = min(float(duration_s), float(chapter_t) + after_s)
    if end <= start:
        end = min(float(duration_s), start + 0.04)
    return quantize_time(start), quantize_time(end)


def clip_filename(t: float, name: str = "") -> str:
    base = f"{quantize_time(t):.3f}"
    if name:
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._-")[:40]
        if safe:
            return f"{base}_{safe}.mp4"
    return f"{base}.mp4"


def extract_clips(
    session: SessionRecord,
    *,
    root: Path,
    redact: bool = False,
    layout_id: str | None = None,
    before_s: float = CLIP_BEFORE_S,
    after_s: float = CLIP_AFTER_S,
) -> ClipResult:
    if not store.is_safe_path_name(session.id):
        raise ValueError(f"unsafe session id {session.id!r}")
    layout = get_layout(layout_id, root=root)
    duration = float(session.recording.duration_s)
    dest_dir = store.clips_dir(root, session.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    times: list[float] = []
    for chapter in session.recording.chapters:
        start, end = clip_window(chapter.t, duration, before_s=before_s, after_s=after_s)
        if end <= start:
            continue
        dest = dest_dir / clip_filename(chapter.t, chapter.name)
        if not store.is_safe_path_name(dest.name):
            raise ValueError(f"unsafe clip name {dest.name!r}")
        _cut_window(session, root, start, end, dest, redact=redact, layout=layout)
        clips.append(dest)
        times.append(quantize_time(chapter.t))
    return ClipResult(session_id=session.id, clips=clips, times=times, redacted=redact)


def _cut_window(
    session: SessionRecord,
    root: Path,
    start: float,
    end: float,
    dest: Path,
    *,
    redact: bool,
    layout: Layout,
) -> None:
    slices = _window_slices(session, root, start, end)
    if not slices:
        raise MediaError(f"no media for clip window [{start}, {end}]")
    vf = drawbox_filter(layout) if redact else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(slices) == 1:
        src, local_start, local_end = slices[0]
        _ffmpeg_cut(src, local_start, local_end, dest, vf=vf)
        return
    with tempfile.TemporaryDirectory(prefix="tva-clip-") as tmp:
        tmp_dir = Path(tmp)
        pieces: list[Path] = []
        for index, (src, local_start, local_end) in enumerate(slices):
            piece = tmp_dir / f"part{index:02d}.mp4"
            _ffmpeg_cut(src, local_start, local_end, piece, vf=None)
            pieces.append(piece)
        joined = tmp_dir / "joined.mp4"
        _concat_copy(pieces, joined)
        if vf:
            _ffmpeg_cut(joined, 0.0, end - start, dest, vf=vf, copy=False)
        else:
            dest.write_bytes(joined.read_bytes())


def _window_slices(
    session: SessionRecord, root: Path, start: float, end: float
) -> list[tuple[Path, float, float]]:
    parts = session.recording.parts
    if not parts:
        src, _ = media_for_time(session, root, start)
        return [(src, start, end)]
    slices: list[tuple[Path, float, float]] = []
    for part in parts:
        part_start = part.offset_s
        part_end = part.offset_s + part.duration_s
        overlap_start = max(start, part_start)
        overlap_end = min(end, part_end)
        if overlap_end <= overlap_start:
            continue
        src = root / part.path
        slices.append((src, overlap_start - part.offset_s, overlap_end - part.offset_s))
    return slices


def _ffmpeg_cut(
    src: Path,
    start: float,
    end: float,
    dest: Path,
    *,
    vf: str | None = None,
    copy: bool = True,
) -> None:
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    if not src.is_file():
        raise FileNotFoundError(src)
    if end <= start:
        raise MediaError(f"empty clip window [{start}, {end}]")
    cmd: list[str] = [
        binary,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-to",
        f"{end:.3f}",
        "-i",
        str(src),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
    ]
    if vf:
        cmd.extend(["-vf", vf, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1"])
    elif copy:
        cmd.extend(["-c", "copy"])
    else:
        cmd.extend(["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1"])
    cmd.append(str(dest))
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise MediaError(result.stderr.strip() or f"ffmpeg clip failed [{start}, {end}]")


def _concat_copy(sources: list[Path], dest: Path) -> None:
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    list_path = dest.with_suffix(".concat.txt")
    lines = [f"file '{src.resolve().as_posix()}'" for src in sources]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    list_path.unlink(missing_ok=True)
    if result.returncode != 0 or not dest.is_file():
        raise MediaError(result.stderr.strip() or "ffmpeg concat clips failed")
