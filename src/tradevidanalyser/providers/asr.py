"""ASR adapters. Default is fake so CI and first-run never call a paid API."""

from __future__ import annotations

import math
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
    except Exception:
        return False
    return True


def cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _import_whisperx() -> Any:
    try:
        import whisperx
    except ImportError as exc:
        raise AsrError(
            "WhisperX is not installed. pip install 'tradevidanalyser[whisperx]' "
            "or set TVA_ASR_PROVIDER=fake."
        ) from exc
    except Exception as exc:
        raise AsrError(f"WhisperX failed to import: {exc}") from exc
    return whisperx


def _pkg_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        mod = sys.modules.get(name)
        return str(getattr(mod, "__version__", "unknown"))


def _parse_device(raw: str) -> tuple[str, int]:
    """Normalize 'cuda', 'cuda:0', 'cpu' → (device, index) for WhisperX/torch."""
    if raw == "cuda" or raw.startswith("cuda:"):
        if raw == "cuda":
            return "cuda", 0
        suffix = raw.split(":", 1)[1]
        try:
            index = int(suffix)
        except ValueError as exc:
            raise AsrError(f"invalid {ENV_ASR_DEVICE}={raw!r}") from exc
        if index < 0:
            raise AsrError(f"invalid {ENV_ASR_DEVICE}={raw!r}")
        return "cuda", index
    return raw, 0


def resolve_device() -> tuple[str, int]:
    override = (os.environ.get(ENV_ASR_DEVICE) or "").strip().lower()
    if override:
        return _parse_device(override)
    return ("cuda", 0) if cuda_available() else ("cpu", 0)


def resolve_compute_type(device: str) -> str:
    override = (os.environ.get(ENV_ASR_COMPUTE_TYPE) or "").strip()
    if override:
        return override
    return "float16" if device == "cuda" else "int8"


def resolve_batch_size(device: str) -> int:
    raw = (os.environ.get(ENV_ASR_BATCH_SIZE) or "").strip()
    if raw:
        try:
            size = int(raw)
        except ValueError as exc:
            raise AsrError(f"invalid {ENV_ASR_BATCH_SIZE}={raw!r}") from exc
        if size < 1:
            raise AsrError(f"{ENV_ASR_BATCH_SIZE} must be >= 1, got {size}")
        return size
    return 16 if device == "cuda" else 8


def _align_device(device: str, device_index: int) -> str:
    if device == "cuda":
        return f"cuda:{device_index}"
    return device


def _finite_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _coerce_text(value: Any) -> str:
    """WhisperX batch_size=1 can leave segment text as a one-item list."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_coerce_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    return str(value).strip()


def _normalize_raw_segments(raw_segments: Any) -> list[dict]:
    out: list[dict] = []
    for raw in raw_segments or []:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        item["text"] = _coerce_text(item.get("text"))
        out.append(item)
    return out


def _word_from_aligned(raw: dict, *, fallback_t0: float, fallback_t1: float) -> TranscriptWord | None:
    if not isinstance(raw, dict):
        return None
    token = _coerce_text(raw.get("word") or raw.get("text"))
    if not token:
        return None
    t0 = _finite_float(raw.get("start"))
    t1 = _finite_float(raw.get("end"))
    if t0 is None:
        t0 = fallback_t0
    if t1 is None:
        t1 = fallback_t1
    score = _finite_float(raw.get("score", raw.get("probability")))
    return TranscriptWord(w=token, t0=t0, t1=t1, p=1.0 if score is None else score)


def transcript_from_whisperx(
    result: dict,
    *,
    language: str = "de",
    model: str,
    prompt_version: str,
) -> Transcript:
    """Map a WhisperX (aligned) result dict onto Transcript with seg_NNN ids."""
    default_lang = _coerce_text(result.get("language")) or language or "de"
    segments: list[TranscriptSegment] = []
    for raw in _normalize_raw_segments(result.get("segments")):
        t0 = _finite_float(raw.get("start"))
        t1 = _finite_float(raw.get("end"))
        lang = _coerce_text(raw.get("language")) or default_lang
        words: list[TranscriptWord] = []
        for item in raw.get("words") or []:
            mapped = _word_from_aligned(
                item,
                fallback_t0=t0 if t0 is not None else 0.0,
                fallback_t1=t1 if t1 is not None else (t0 if t0 is not None else 0.0),
            )
            if mapped is not None:
                words.append(mapped)
        text = raw.get("text") or ""
        if not text:
            text = " ".join(word.w for word in words).strip()
        if not text:
            continue
        if t0 is None:
            t0 = words[0].t0 if words else 0.0
        if t1 is None:
            t1 = words[-1].t1 if words else t0
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
        device, device_index = resolve_device()
        compute_type = resolve_compute_type(device)
        batch_size = resolve_batch_size(device)
        model_name = (os.environ.get(ENV_ASR_MODEL) or self.model).strip() or self.model
        vad_method = (os.environ.get(ENV_ASR_VAD) or "silero").strip() or "silero"
        wx_ver = _pkg_version("whisperx")
        prompt_version = f"jargon-v1+{model_name}+whisperx-{wx_ver}"
        align_device = _align_device(device, device_index)

        if device == "cpu":
            warnings.warn(
                f"WhisperX is running on CPU ({compute_type}). This is slow; prefer the trading "
                "PC CUDA GPU or a hosted ASR provider (PR-05).",
                stacklevel=2,
            )

        asr_options = {"initial_prompt": glossary.initial_prompt}
        try:
            load_kwargs: dict[str, Any] = {
                "compute_type": compute_type,
                "language": language,
                "asr_options": asr_options,
                "device_index": device_index,
            }
            try:
                model = wx.load_model(model_name, device, vad_method=vad_method, **load_kwargs)
            except TypeError:
                # Older whisperx builds have no vad_method kwarg (defaulted to pyannote).
                model = wx.load_model(model_name, device, **load_kwargs)
            waveform = wx.load_audio(str(audio))
            result = model.transcribe(waveform, batch_size=batch_size, language=language)
            segments = [seg for seg in _normalize_raw_segments(result.get("segments")) if seg.get("text")]
            detected = _coerce_text(result.get("language")) or language or "de"
            if segments:
                align_model, metadata = wx.load_align_model(
                    language_code=detected,
                    device=align_device,
                )
                aligned = wx.align(
                    segments,
                    align_model,
                    metadata,
                    waveform,
                    align_device,
                    return_char_alignments=False,
                )
            else:
                aligned = {"segments": [], "language": detected}
            if not isinstance(aligned, dict):
                raise AsrError("WhisperX align() returned a non-dict result")
            if "language" not in aligned:
                aligned["language"] = detected
            return transcript_from_whisperx(
                aligned,
                language=language,
                model=model_name,
                prompt_version=prompt_version,
            )
        except AsrError:
            raise
        except Exception as exc:
            raise AsrError(f"WhisperX failed on {audio}: {exc}") from exc


def get_asr_provider(name: str | None = None) -> AsrProvider:
    chosen = (name or os.environ.get("TVA_ASR_PROVIDER") or "fake").strip().lower() or "fake"
    if chosen in {"fake", "test"}:
        return FakeAsrProvider()
    if chosen in {"whisperx", "whisper"}:
        return WhisperXAsrProvider()
    raise AsrError(f"unknown ASR provider {chosen!r}")
