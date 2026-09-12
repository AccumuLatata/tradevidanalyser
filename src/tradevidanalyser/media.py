"""ffprobe / ffmpeg / hashing. Fail closed if the tools are missing."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from tradevidanalyser.schema import Chapter


class MediaError(RuntimeError):
    pass


def which(name: str) -> str | None:
    return shutil.which(name)


def sha256_file(path: Path, *, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(chunk)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def ffprobe(path: Path) -> dict[str, Any]:
    binary = which("ffprobe")
    if not binary:
        raise MediaError("ffprobe not on PATH")
    result = subprocess.run(
        [
            binary,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout or "{}")


def duration_s(probe: dict[str, Any]) -> float:
    fmt = probe.get("format") or {}
    raw = fmt.get("duration")
    if raw is not None:
        return float(raw)
    for stream in probe.get("streams") or []:
        if stream.get("duration"):
            return float(stream["duration"])
    return 0.0


def audio_track_labels(probe: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for index, stream in enumerate(probe.get("streams") or []):
        if stream.get("codec_type") != "audio":
            continue
        tags = stream.get("tags") or {}
        title = tags.get("title") or tags.get("handler_name") or f"a:{index}"
        labels.append(str(title))
    if labels:
        return labels
    return []


def chapters_from_probe(probe: dict[str, Any]) -> list[Chapter]:
    out: list[Chapter] = []
    for raw in probe.get("chapters") or []:
        start = raw.get("start_time")
        if start is None:
            continue
        tags = raw.get("tags") or {}
        out.append(Chapter(t=float(start), name=str(tags.get("title") or "")))
    return out


def extract_track(src: Path, index: int, dest: Path) -> None:
    """Extract audio stream ``0:a:{index}`` as mono Opus."""
    if index < 0:
        raise MediaError(f"audio track index must be >= 0, got {index}")
    binary = which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            binary,
            "-y",
            "-i",
            str(src),
            "-map",
            f"0:a:{index}",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ac",
            "1",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or f"ffmpeg extract 0:a:{index} failed")


def extract_mic_opus(src: Path, dest: Path) -> None:
    extract_track(src, 0, dest)


def concat_audio(sources: list[Path], dest: Path) -> None:
    """Concatenate audio files in order into one Opus file."""
    if not sources:
        raise MediaError("concat_audio requires at least one source")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if len(sources) == 1:
        if sources[0].resolve() != dest.resolve():
            dest.write_bytes(sources[0].read_bytes())
        return
    binary = which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    cmd: list[str] = [binary, "-y"]
    for src in sources:
        cmd.extend(["-i", str(src)])
    n = len(sources)
    labels = "".join(f"[{i}:a:0]" for i in range(n))
    cmd.extend(
        [
            "-filter_complex",
            f"{labels}concat=n={n}:v=0:a=1[out]",
            "-map",
            "[out]",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-ac",
            "1",
            str(dest),
        ]
    )
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        raise MediaError(result.stderr.strip() or "ffmpeg concat audio failed")
