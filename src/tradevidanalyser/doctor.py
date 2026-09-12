"""Environment checks a bot or human can read."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from tradevidanalyser import __version__, media
from tradevidanalyser.providers.asr import cuda_available, whisperx_importable
from tradevidanalyser.schema import DoctorCheck, DoctorReport


def run_doctor(root: Path) -> DoctorReport:
    checks: list[DoctorCheck] = []

    checks.append(
        DoctorCheck(
            id="python",
            status="ok" if sys.version_info >= (3, 11) else "fail",
            detail=f"{sys.version.split()[0]} (need >= 3.11)",
        )
    )

    ffprobe = media.which("ffprobe")
    ffmpeg = media.which("ffmpeg")
    checks.append(
        DoctorCheck(
            id="ffprobe",
            status="ok" if ffprobe else "fail",
            detail=ffprobe or "not on PATH",
        )
    )
    checks.append(
        DoctorCheck(
            id="ffmpeg",
            status="ok" if ffmpeg else "fail",
            detail=ffmpeg or "not on PATH",
        )
    )

    root_ok = root.exists()
    writable = False
    if root_ok:
        writable = os.access(root, os.W_OK)
    else:
        parent = root.parent
        writable = parent.exists() and os.access(parent, os.W_OK)
    if root_ok and writable:
        status = "ok"
        detail = str(root)
    elif writable:
        status = "warn"
        detail = f"{root} does not exist yet; parent is writable"
    else:
        status = "fail"
        detail = f"{root} missing or not writable"
    checks.append(DoctorCheck(id="tva_root", status=status, detail=detail))

    gpu = shutil.which("nvidia-smi")
    checks.append(
        DoctorCheck(
            id="gpu",
            status="ok" if gpu else "warn",
            detail="nvidia-smi found" if gpu else "no NVIDIA GPU tools; use PC GPU or hosted ASR",
        )
    )

    xai = bool(os.environ.get("XAI_API_KEY"))
    checks.append(
        DoctorCheck(
            id="xai_key",
            status="ok" if xai else "warn",
            detail="XAI_API_KEY set" if xai else "XAI_API_KEY unset (extraction uses fake unless set)",
        )
    )

    provider = os.environ.get("TVA_ASR_PROVIDER", "fake")
    checks.append(
        DoctorCheck(
            id="asr_provider",
            status="ok" if provider in {"fake", "whisperx"} else "warn",
            detail=f"TVA_ASR_PROVIDER={provider}",
        )
    )

    wx_ok = whisperx_importable()
    if wx_ok:
        wx_status, wx_detail = "ok", "whisperx importable"
    elif provider in {"whisperx", "whisper"}:
        wx_status = "fail"
        wx_detail = "TVA_ASR_PROVIDER=whisperx but whisperx is not installed"
    else:
        wx_status, wx_detail = "warn", "whisperx not installed (pip install 'tradevidanalyser[whisperx]')"
    checks.append(DoctorCheck(id="whisperx", status=wx_status, detail=wx_detail))

    cuda_ok = cuda_available()
    checks.append(
        DoctorCheck(
            id="cuda",
            status="ok" if cuda_ok else "warn",
            detail=(
                "torch.cuda.is_available()"
                if cuda_ok
                else "no CUDA; WhisperX uses CPU int8 (slow on Mac — prefer PC GPU or hosted ASR)"
            ),
        )
    )

    ok = all(c.status != "fail" for c in checks)
    return DoctorReport(
        app_version=__version__,
        root=str(root),
        checks=checks,
        ok=ok,
    )
