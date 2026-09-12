from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tradevidanalyser import config


@pytest.fixture
def tva_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "store"
    config.ensure_layout(root)
    monkeypatch.setenv("TVA_ROOT", str(root))
    return root


def make_test_video(
    dest: Path,
    *,
    seconds: float = 1.0,
    chapters: list[tuple[float, str]] | None = None,
    extra_audio: bool = False,
) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    raw = dest if not chapters else dest.with_name(dest.stem + ".__raw__.mp4")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=160x120:d={seconds}",
    ]
    if extra_audio:
        cmd.extend(["-f", "lavfi", "-i", f"sine=frequency=880:duration={seconds}"])
    cmd.extend(["-map", "1:v:0", "-map", "0:a:0"])
    if extra_audio:
        cmd.extend(["-map", "2:a:0"])
    cmd.extend(
        [
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
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


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    return make_test_video(tmp_path / "2026-09-11 14-30-00.mp4")


@pytest.fixture
def write_video():
    return make_test_video
