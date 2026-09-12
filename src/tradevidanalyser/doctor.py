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

    xai = bool((os.environ.get("XAI_API_KEY") or "").strip())
    checks.append(
        DoctorCheck(
            id="xai_key",
            status="ok" if xai else "warn",
            detail="XAI_API_KEY set" if xai else "XAI_API_KEY unset (needed for TVA_EXTRACT_PROVIDER=grok)",
        )
    )

    extract = (os.environ.get("TVA_EXTRACT_PROVIDER") or "fake").strip().lower() or "fake"
    if extract in {"grok", "xai"}:
        if xai:
            ex_status, ex_detail = "ok", f"TVA_EXTRACT_PROVIDER={extract}"
        else:
            ex_status, ex_detail = "fail", "TVA_EXTRACT_PROVIDER=grok but XAI_API_KEY is unset"
    elif extract in {"fake", "test", "keyword"}:
        ex_status, ex_detail = "ok", f"TVA_EXTRACT_PROVIDER={extract}"
    else:
        ex_status, ex_detail = "warn", f"TVA_EXTRACT_PROVIDER={extract}"
    checks.append(DoctorCheck(id="extract_provider", status=ex_status, detail=ex_detail))

    provider = (os.environ.get("TVA_ASR_PROVIDER") or "fake").strip().lower() or "fake"
    known = {"fake", "whisperx", "whisper", "deepgram", "hosted"}
    if provider in {"scribe", "elevenlabs"}:
        asr_status, asr_detail = "warn", f"TVA_ASR_PROVIDER={provider}; PR-05 pick is deepgram"
    elif provider in known:
        asr_status, asr_detail = "ok", f"TVA_ASR_PROVIDER={provider}"
    else:
        asr_status, asr_detail = "warn", f"TVA_ASR_PROVIDER={provider}"
    checks.append(DoctorCheck(id="asr_provider", status=asr_status, detail=asr_detail))

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

    dg_key = bool((os.environ.get("DEEPGRAM_API_KEY") or "").strip())
    if dg_key:
        dg_status, dg_detail = "ok", "DEEPGRAM_API_KEY set"
    elif provider in {"deepgram", "hosted"}:
        dg_status, dg_detail = "fail", f"TVA_ASR_PROVIDER={provider} but DEEPGRAM_API_KEY is unset"
    else:
        dg_status, dg_detail = "warn", "DEEPGRAM_API_KEY unset (hosted ASR uses fake unless set)"
    checks.append(DoctorCheck(id="deepgram_key", status=dg_status, detail=dg_detail))

    el_key = bool((os.environ.get("ELEVENLABS_API_KEY") or "").strip())
    checks.append(
        DoctorCheck(
            id="elevenlabs_key",
            status="ok" if el_key else "warn",
            detail=(
                "ELEVENLABS_API_KEY set"
                if el_key
                else "ELEVENLABS_API_KEY unset (Scribe is not the PR-05 pick)"
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
