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


def extract_mic_opus(src: Path, dest: Path) -> None:
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
            "0:a:0",
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
        raise MediaError(result.stderr.strip() or "ffmpeg audio extract failed")
