"""Black-box ``*_mask`` ROIs before a frame is saved or a clip leaves the box."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tradevidanalyser import media
from tradevidanalyser.frames import Layout, Roi
from tradevidanalyser.media import MediaError

MASK_SUFFIX = "_mask"


def mask_rois(layout: Layout) -> dict[str, Roi]:
    """Return named mask ROIs with a non-zero area."""
    return {
        name: roi
        for name, roi in layout.rois.items()
        if name.endswith(MASK_SUFFIX) and roi.w > 0 and roi.h > 0
    }


def drawbox_filter(layout: Layout) -> str | None:
    """ffmpeg ``drawbox`` chain in normalised ``iw``/``ih`` units, or None."""
    filters: list[str] = []
    for roi in mask_rois(layout).values():
        filters.append(
            "drawbox="
            f"x=iw*{roi.x}:y=ih*{roi.y}:w=iw*{roi.w}:h=ih*{roi.h}:"
            "color=black:t=fill"
        )
    return ",".join(filters) if filters else None


def redact_image(src: Path, dest: Path, layout: Layout) -> Path:
    """Overwrite ``dest`` with ``src`` after applying mask drawboxes."""
    vf = drawbox_filter(layout)
    if vf is None:
        if src.resolve() != dest.resolve():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(src.read_bytes())
        return dest
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".__redact__.jpg")
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vf",
            vf,
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(tmp),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise MediaError(result.stderr.strip() or "ffmpeg redact frame failed")
    tmp.replace(dest)
    return dest
