"""Chapter keyframes and screen-layout ROIs. Raw JPEGs stay under TVA_ROOT."""

from __future__ import annotations

import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from tradevidanalyser import media, store
from tradevidanalyser.media import MediaError
from tradevidanalyser.schema import SessionRecord

SCHEMA_VERSION = "1"
REQUIRED_ROIS = (
    "clock",
    "position",
    "pnl",
    "instrument",
    "account_mask",
    "balance_mask",
)
FRAME_OFFSETS_S = (-5.0, -2.0, 0.0, 2.0, 5.0)
CONTACT_SHEET_NAME = "contact_sheet.jpg"
DEFAULT_LAYOUT_ID = "quantower_default"
ENV_LAYOUT = "TVA_LAYOUT"

_ROI_INLINE = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*:\s*"
    r"\{(?P<body>[^}]*)\}\s*$"
)
_ROI_FIELD = re.compile(r"(?P<k>[xywh])\s*:\s*(?P<v>[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)")
_LAYOUT_ID = re.compile(r"^(\s{2}|\t)([A-Za-z_][A-Za-z0-9_]*)\s*:\s*$")
_DESCRIPTION = re.compile(r"^\s+description\s*:\s*(.*)$")
_ROIS_HEADER = re.compile(r"^\s+rois\s*:\s*$")


@dataclass(frozen=True)
class Roi:
    x: float
    y: float
    w: float
    h: float


@dataclass(frozen=True)
class Layout:
    layout_id: str
    rois: dict[str, Roi]
    description: str = ""

    def require_rois(self) -> None:
        missing = [name for name in REQUIRED_ROIS if name not in self.rois]
        if missing:
            raise ValueError(f"layout {self.layout_id} missing rois: {', '.join(missing)}")


@dataclass(frozen=True)
class FramesResult:
    session_id: str
    layout_id: str
    times: list[float]
    frames: list[Path]
    contact_sheet: Path | None

    def as_dict(self, root: Path) -> dict:
        return {
            "session_id": self.session_id,
            "layout_id": self.layout_id,
            "times": self.times,
            "frames": [_rel(path, root) for path in self.frames],
            "contact_sheet": _rel(self.contact_sheet, root) if self.contact_sheet else None,
            "count": len(self.frames),
        }


def default_layout_path(*, root: Path | None = None) -> Path:
    env = (os.environ.get(ENV_LAYOUT) or "").strip()
    if env:
        return Path(env).expanduser()
    candidates = []
    if root is not None:
        candidates.append(Path(root) / "layout.yaml")
    here = Path(__file__).resolve().parent
    candidates.append(here / "layout.yaml")
    if len(here.parents) >= 2:
        candidates.append(here.parents[1] / "layout.yaml")  # repo-root layout.yaml in a checkout
    candidates.append(Path.cwd() / "layout.yaml")
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("layout.yaml not found (set TVA_LAYOUT or keep the shipped file)")


def load_layouts(path: Path | None = None, *, root: Path | None = None) -> dict[str, Layout]:
    resolved = path or default_layout_path(root=root)
    layouts = _parse_layout_yaml(resolved.read_text(encoding="utf-8"))
    if not layouts:
        raise ValueError(f"{resolved} has no layouts")
    return layouts


def get_layout(
    layout_id: str | None = None, *, path: Path | None = None, root: Path | None = None
) -> Layout:
    layouts = load_layouts(path, root=root)
    wanted = layout_id or DEFAULT_LAYOUT_ID
    if wanted not in layouts:
        if layout_id is None and len(layouts) == 1:
            layout = next(iter(layouts.values()))
        else:
            raise ValueError(f"unknown layout_id {wanted!r}")
    else:
        layout = layouts[wanted]
    layout.require_rois()
    return layout


def quantize_time(t: float) -> float:
    return round(float(t) + 0.0, 3)


def default_frame_times(session: SessionRecord) -> list[float]:
    duration = float(session.recording.duration_s)
    times: set[float] = set()
    for chapter in session.recording.chapters:
        for offset in FRAME_OFFSETS_S:
            t = chapter.t + offset
            if 0.0 <= t <= duration + 1e-9:
                times.add(quantize_time(t))
    return sorted(times)


def media_for_time(session: SessionRecord, root: Path, t: float) -> tuple[Path, float]:
    """Return ``(media_path, local_seek_s)`` for session time ``t``."""
    parts = session.recording.parts
    if parts:
        last = parts[-1]
        for index, part in enumerate(parts):
            start = part.offset_s
            end = start + part.duration_s
            is_last = index == len(parts) - 1
            if start <= t < end or (is_last and t >= start):
                return root / part.path, _clamp_seek(t - start, part.duration_s)
        return root / last.path, _clamp_seek(t - last.offset_s, last.duration_s)
    return root / session.recording.path, _clamp_seek(t, session.recording.duration_s)


def extract_frames(
    session: SessionRecord,
    times: Sequence[float] | None = None,
    *,
    root: Path,
    contact_sheet: bool = False,
    layout_id: str | None = None,
) -> FramesResult:
    layout = get_layout(layout_id, root=root)
    chosen = [quantize_time(t) for t in (times if times is not None else default_frame_times(session))]
    chosen = sorted(dict.fromkeys(t for t in chosen if t >= 0.0))
    dest_dir = store.frames_dir(root, session.id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Path] = []
    for t in chosen:
        src, seek = media_for_time(session, root, t)
        dest = store.frame_path(root, session.id, t)
        _extract_one(src, seek, dest)
        frames.append(dest)
    sheet: Path | None = None
    if contact_sheet:
        if not frames:
            raise ValueError("contact sheet requested but no frames were extracted")
        sheet = store.contact_sheet_path(root, session.id)
        write_contact_sheet(frames, sheet)
    return FramesResult(
        session_id=session.id,
        layout_id=layout.layout_id,
        times=chosen,
        frames=frames,
        contact_sheet=sheet,
    )


def write_contact_sheet(frames: Sequence[Path], dest: Path) -> Path:
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    if not frames:
        raise ValueError("contact sheet requires at least one frame")
    dest.parent.mkdir(parents=True, exist_ok=True)
    n = len(frames)
    if n == 1:
        dest.write_bytes(frames[0].read_bytes())
        return dest
    cols = min(5, n)
    rows = math.ceil(n / cols)
    cmd: list[str] = [binary, "-hide_banner", "-loglevel", "error", "-y"]
    for path in frames:
        cmd.extend(["-i", str(path)])
    # `tile` stacks successive frames of one stream; multiple JPEGs need xstack.
    scaled = [
        f"[{i}:v]scale=320:240:force_original_aspect_ratio=decrease,"
        f"pad=320:240:(ow-iw)/2:(oh-ih)/2,setsar=1[v{i}]"
        for i in range(n)
    ]
    stacked = "".join(f"[v{i}]" for i in range(n))
    scaled.append(f"{stacked}xstack=inputs={n}:grid={cols}x{rows}:fill=black[out]")
    cmd.extend(
        [
            "-filter_complex",
            ";".join(scaled),
            "-map",
            "[out]",
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(dest),
        ]
    )
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise MediaError(result.stderr.strip() or "ffmpeg contact sheet failed")
    return dest


def accurate_seek_cmd(ffmpeg: str, src: Path, seek_s: float, dest: Path) -> list[str]:
    """Accurate seek: ``-ss`` after ``-i`` so the decode lands on the timestamp."""
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-ss",
        f"{seek_s:.3f}",
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(dest),
    ]


def _extract_one(src: Path, seek_s: float, dest: Path) -> None:
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    if not src.is_file():
        raise FileNotFoundError(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = accurate_seek_cmd(binary, src, seek_s, dest)
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise MediaError(result.stderr.strip() or f"ffmpeg frame extract failed at {seek_s}")


def _clamp_seek(local: float, duration_s: float) -> float:
    if duration_s <= 0:
        return 0.0
    ceiling = max(duration_s - 0.04, 0.0)
    return min(max(local, 0.0), ceiling)


def _rel(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _parse_layout_yaml(text: str) -> dict[str, Layout]:
    schema: str | None = None
    layouts: dict[str, Layout] = {}
    current_id: str | None = None
    current_desc = ""
    current_rois: dict[str, Roi] = {}
    in_layouts = False
    in_rois = False

    def flush() -> None:
        nonlocal current_id, current_desc, current_rois, in_rois
        if current_id:
            layouts[current_id] = Layout(
                layout_id=current_id,
                rois=dict(current_rois),
                description=current_desc,
            )
        current_id = None
        current_desc = ""
        current_rois = {}
        in_rois = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        if stripped.startswith("schema_version"):
            _, _, value = stripped.partition(":")
            schema = value.strip().strip("\"'")
            continue
        if stripped == "layouts:":
            in_layouts = True
            continue
        if not in_layouts:
            continue
        match_id = _LAYOUT_ID.match(line)
        if match_id:
            flush()
            current_id = match_id.group(2)
            continue
        if current_id is None:
            continue
        match_desc = _DESCRIPTION.match(line)
        if match_desc:
            current_desc = match_desc.group(1).strip().strip("\"'")
            continue
        if _ROIS_HEADER.match(line):
            in_rois = True
            continue
        if in_rois:
            match_roi = _ROI_INLINE.match(line)
            if not match_roi:
                raise ValueError(f"unreadable roi line: {raw}")
            name = match_roi.group("name")
            current_rois[name] = _parse_roi_body(name, match_roi.group("body"))
    flush()
    if schema is not None and schema != SCHEMA_VERSION:
        raise ValueError(f"unsupported layout schema_version {schema!r}")
    return layouts


def _parse_roi_body(name: str, body: str) -> Roi:
    found = {match.group("k"): float(match.group("v")) for match in _ROI_FIELD.finditer(body)}
    missing = [key for key in ("x", "y", "w", "h") if key not in found]
    if missing:
        raise ValueError(f"roi {name} missing {', '.join(missing)}")
    roi = Roi(x=found["x"], y=found["y"], w=found["w"], h=found["h"])
    for field, value in (("x", roi.x), ("y", roi.y), ("w", roi.w), ("h", roi.h)):
        if not 0.0 <= value <= 1.0:
            raise ValueError(f"layout roi {name}.{field}={value} is outside 0..1")
    return roi
