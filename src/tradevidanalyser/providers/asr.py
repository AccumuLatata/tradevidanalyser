"""ASR adapters. Default is fake so CI and first-run never call a paid API."""

from __future__ import annotations

import os
import sys
import warnings
from importlib.metadata import PackageNotFoundError, version as pkg_version
from pathlib import Path
from typing import Any, Protocol

from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.schema import Transcript, TranscriptSegment, TranscriptWord

DEFAULT_WHISPERX_MODEL = "large-v3"
WORD_TIME_TOLERANCE_S = 0.020
ENV_ASR_MODEL = "TVA_ASR_MODEL"
ENV_ASR_BATCH_SIZE = "TVA_ASR_BATCH_SIZE"
ENV_ASR_DEVICE = "TVA_ASR_DEVICE"
ENV_ASR_COMPUTE_TYPE = "TVA_ASR_COMPUTE_TYPE"
ENV_ASR_VAD = "TVA_ASR_VAD"


class AsrError(RuntimeError):
    pass


class AsrProvider(Protocol):
    name: str
    model: str

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript: ...


class FakeAsrProvider:
    """Deterministic German placeholder. Used in tests and when no GPU/key."""

    name = "fake"
    model = "fake-v1"

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript:
        sidecar = audio.with_suffix(".transcript.json")
        if sidecar.is_file():
            return Transcript.model_validate_json(sidecar.read_text(encoding="utf-8"))
        # Canned text stays deterministic for CI; the glossary load checks that
        # FakeAsr is wired to docs/GLOSSARY.md (tokens feed later WhisperX).
        glossary = load_glossary()
        if not glossary.tokens:
            raise AsrError("docs/GLOSSARY.md produced an empty token list")
        text = "Bias long. Playbook ONH Touch. Stop unter dem Level. Check-in zur halben Stunde."
        words: list[TranscriptWord] = []
        cursor = 0.0
        for token in text.replace(".", " .").split():
            words.append(TranscriptWord(w=token, t0=cursor, t1=cursor + 0.4, p=1.0))
            cursor += 0.4
        segment = TranscriptSegment(
            id="seg_001",
            t0=0.0,
            t1=cursor,
            lang=language,
            text=text,
            words=words,
        )
        return Transcript(
            provider=self.name,
            model=self.model,
            language=language,
            segments=[segment],
        )


def whisperx_importable() -> bool:
    try:
        import whisperx  # noqa: F401
    except ImportError:
        return False
    return True


def cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except ImportError:
        return False


def _import_whisperx() -> Any:
    try:
        import whisperx
    except ImportError as exc:
        raise AsrError(
            "WhisperX is not installed. pip install 'tradevidanalyser[whisperx]' "
            "or set TVA_ASR_PROVIDER=fake."
        ) from exc
    return whisperx


def _pkg_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        mod = sys.modules.get(name)
        return str(getattr(mod, "__version__", "unknown"))


def resolve_device() -> str:
    override = (os.environ.get(ENV_ASR_DEVICE) or "").strip().lower()
    if override:
        return override
    return "cuda" if cuda_available() else "cpu"


def resolve_compute_type(device: str) -> str:
    override = (os.environ.get(ENV_ASR_COMPUTE_TYPE) or "").strip()
    if override:
        return override
    return "float16" if device == "cuda" else "int8"


def resolve_batch_size(device: str) -> int:
    raw = (os.environ.get(ENV_ASR_BATCH_SIZE) or "").strip()
    if raw:
        return int(raw)
    return 16 if device == "cuda" else 8


def _word_from_aligned(raw: dict, *, fallback_t0: float, fallback_t1: float) -> TranscriptWord | None:
    token = str(raw.get("word") or raw.get("text") or "").strip()
    if not token:
        return None
    t0 = raw.get("start")
    t1 = raw.get("end")
    if t0 is None or t1 is None:
        t0 = fallback_t0
        t1 = fallback_t1
    score = raw.get("score", raw.get("probability", 1.0))
    return TranscriptWord(
        w=token,
        t0=float(t0),
        t1=float(t1),
        p=float(score if score is not None else 1.0),
    )


def transcript_from_whisperx(
    result: dict,
    *,
    language: str = "de",
    model: str,
    prompt_version: str,
) -> Transcript:
    """Map a WhisperX (aligned) result dict onto Transcript with seg_NNN ids."""
    default_lang = str(result.get("language") or language or "de")
    segments: list[TranscriptSegment] = []
    for raw in result.get("segments") or []:
        text = str(raw.get("text") or "").strip()
        if not text:
            continue
        t0 = float(raw.get("start") if raw.get("start") is not None else 0.0)
        t1 = float(raw.get("end") if raw.get("end") is not None else t0)
        lang = str(raw.get("language") or default_lang or "de")
        words: list[TranscriptWord] = []
        for item in raw.get("words") or []:
            mapped = _word_from_aligned(item, fallback_t0=t0, fallback_t1=t1)
            if mapped is not None:
                words.append(mapped)
        segments.append(
            TranscriptSegment(
                id=f"seg_{len(segments) + 1:03d}",
                t0=t0,
                t1=t1,
                lang=lang,
                text=text,
                words=words,
            )
        )
    return Transcript(
        provider="whisperx",
        model=model,
        prompt_version=prompt_version,
        language=default_lang,
        segments=segments,
    )


def transcripts_close(
    first: Transcript,
    second: Transcript,
    *,
    word_tol_s: float = WORD_TIME_TOLERANCE_S,
) -> bool:
    """True if two runs match modulo floating word/segment times within *word_tol_s*."""
    if (
        first.provider != second.provider
        or first.model != second.model
        or first.prompt_version != second.prompt_version
        or first.language != second.language
        or len(first.segments) != len(second.segments)
    ):
        return False
    for left, right in zip(first.segments, second.segments, strict=True):
        if left.id != right.id or left.text != right.text or left.lang != right.lang:
            return False
        if abs(left.t0 - right.t0) > word_tol_s or abs(left.t1 - right.t1) > word_tol_s:
            return False
        if len(left.words) != len(right.words):
            return False
        for w_left, w_right in zip(left.words, right.words, strict=True):
            if w_left.w != w_right.w:
                return False
            if abs(w_left.t0 - w_right.t0) > word_tol_s or abs(w_left.t1 - w_right.t1) > word_tol_s:
                return False
    return True


class WhisperXAsrProvider:
    """Local WhisperX adapter. Opt-in via TVA_ASR_PROVIDER=whisperx. No diarization."""

    name = "whisperx"
    model = DEFAULT_WHISPERX_MODEL

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript:
        wx = _import_whisperx()
        glossary = load_glossary()
        if not glossary.tokens:
            raise AsrError("docs/GLOSSARY.md produced an empty token list")

        language = language or "de"
        device = resolve_device()
        compute_type = resolve_compute_type(device)
        batch_size = resolve_batch_size(device)
        model_name = (os.environ.get(ENV_ASR_MODEL) or self.model).strip() or self.model
        vad_method = (os.environ.get(ENV_ASR_VAD) or "silero").strip() or "silero"
        wx_ver = _pkg_version("whisperx")
        prompt_version = f"jargon-v1+{model_name}+whisperx-{wx_ver}"

        if device == "cpu":
            warnings.warn(
                "WhisperX is running on CPU (int8). This is slow; prefer the trading "
                "PC CUDA GPU or a hosted ASR provider (PR-05).",
                stacklevel=2,
            )

        asr_options = {"initial_prompt": glossary.initial_prompt}
        try:
            model = wx.load_model(
                model_name,
                device,
                compute_type=compute_type,
                language=language,
                asr_options=asr_options,
                vad_method=vad_method,
            )
            waveform = wx.load_audio(str(audio))
            result = model.transcribe(waveform, batch_size=batch_size)
            segments = list(result.get("segments") or [])
            detected = str(result.get("language") or language or "de")
            if segments:
                align_model, metadata = wx.load_align_model(
                    language_code=detected,
                    device=device,
                )
                aligned = wx.align(
                    segments,
                    align_model,
                    metadata,
                    waveform,
                    device,
                    return_char_alignments=False,
                )
            else:
                aligned = {"segments": [], "language": detected}
            if "language" not in aligned:
                aligned["language"] = detected
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"WhisperX failed on {audio}: {exc}") from exc

        return transcript_from_whisperx(
            aligned,
            language=language,
            model=model_name,
            prompt_version=prompt_version,
        )


def get_asr_provider(name: str | None = None) -> AsrProvider:
    chosen = (name or os.environ.get("TVA_ASR_PROVIDER") or "fake").strip().lower()
    if chosen in {"fake", "test"}:
        return FakeAsrProvider()
    if chosen in {"whisperx", "whisper"}:
        return WhisperXAsrProvider()
    raise AsrError(f"unknown ASR provider {chosen!r}")
