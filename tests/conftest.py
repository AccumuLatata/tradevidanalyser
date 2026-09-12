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


def make_test_video(dest: Path, *, seconds: float = 1.0) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
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
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return dest


@pytest.fixture
def sample_video(tmp_path: Path) -> Path:
    return make_test_video(tmp_path / "2026-09-11 14-30-00.mp4")


@pytest.fixture
def write_video():
    return make_test_video
