"""Hosted ASR adapters. PR-05 pick is Deepgram; Scribe keeps the same interface."""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

import httpx

from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.providers.asr import AsrError
from tradevidanalyser.schema import Transcript, TranscriptSegment, TranscriptWord

DEEPGRAM_LISTEN_URL = "https://api.deepgram.com/v1/listen"
DEFAULT_DEEPGRAM_MODEL = "nova-3"
DEEPGRAM_USD_PER_AUDIO_HOUR = 0.29
MAX_KEYTERMS = 80
ENV_DEEPGRAM_KEY = "DEEPGRAM_API_KEY"
ENV_ELEVENLABS_KEY = "ELEVENLABS_API_KEY"
ENV_USD_PER_HOUR = "TVA_ASR_USD_PER_HOUR"

_VIDEO_SUFFIXES = {".mp4", ".mkv", ".mov", ".avi", ".webm"}
_FRAME_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif", ".heic"}
_AUDIO_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}


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
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        parts = [_coerce_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    return str(value).strip()


def estimate_cost_usd(duration_s: float, *, usd_per_hour: float | None = None) -> float:
    rate = _finite_float(usd_per_hour)
    if rate is None:
        raw = (os.environ.get(ENV_USD_PER_HOUR) or "").strip()
        rate = _finite_float(raw) if raw else DEEPGRAM_USD_PER_AUDIO_HOUR
    if rate is None or rate < 0:
        rate = DEEPGRAM_USD_PER_AUDIO_HOUR
    duration = _finite_float(duration_s)
    hours = max(duration if duration is not None else 0.0, 0.0) / 3600.0
    return round(hours * rate, 6)


def glossary_keyterms(limit: int = MAX_KEYTERMS) -> list[str]:
    seen: list[str] = []
    if limit <= 0:
        return seen
    for token in load_glossary().tokens:
        if token and token not in seen:
            seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _content_type(audio: Path) -> str:
    suffix = audio.suffix.lower()
    if suffix in _AUDIO_TYPES:
        return _AUDIO_TYPES[suffix]
    if suffix in _VIDEO_SUFFIXES:
        raise AsrError("hosted ASR uploads audio only; pass audio/mic.opus, not video")
    if suffix in _FRAME_SUFFIXES:
        raise AsrError("hosted ASR uploads audio only; frames must not leave the machine")
    raise AsrError("hosted ASR uploads audio only; pass audio/mic.opus")


def _word(raw: Any, *, fallback_t0: float, fallback_t1: float) -> TranscriptWord | None:
    if not isinstance(raw, dict):
        return None
    token = _coerce_text(raw.get("punctuated_word") or raw.get("word"))
    if not token:
        return None
    t0 = _finite_float(raw.get("start"))
    t1 = _finite_float(raw.get("end"))
    if t0 is None:
        t0 = fallback_t0
    if t1 is None:
        t1 = fallback_t1
    confidence = _finite_float(raw.get("confidence"))
    return TranscriptWord(w=token, t0=t0, t1=t1, p=1.0 if confidence is None else confidence)


def _utterances_from_payload(payload: dict) -> list[dict]:
    results = payload.get("results")
    if not isinstance(results, dict):
        return []
    raw_utterances = results.get("utterances") or []
    utterances = [item for item in raw_utterances if isinstance(item, dict)]
    if utterances:
        return utterances

    channels = results.get("channels") or []
    if not channels or not isinstance(channels[0], dict):
        return []
    alternatives = channels[0].get("alternatives") or []
    alt = alternatives[0] if alternatives and isinstance(alternatives[0], dict) else {}
    text = _coerce_text(alt.get("transcript"))
    words = [item for item in (alt.get("words") or []) if isinstance(item, dict)]
    if not text and not words:
        return []
    first_start = _finite_float(words[0].get("start")) if words else 0.0
    last_end = _finite_float(words[-1].get("end")) if words else first_start
    return [
        {
            "start": first_start if first_start is not None else 0.0,
            "end": last_end if last_end is not None else 0.0,
            "transcript": text,
            "words": words,
        }
    ]


def transcript_from_deepgram(
    payload: dict,
    *,
    language: str = "de",
    model: str = DEFAULT_DEEPGRAM_MODEL,
    prompt_version: str,
) -> Transcript:
    """Map a Deepgram listen response onto Transcript with seg_NNN ids."""
    if not isinstance(payload, dict):
        raise AsrError("Deepgram returned a non-object JSON body")
    language = language or "de"
    segments: list[TranscriptSegment] = []
    for raw in _utterances_from_payload(payload):
        words_raw = [item for item in (raw.get("words") or []) if isinstance(item, dict)]
        t0 = _finite_float(raw.get("start"))
        t1 = _finite_float(raw.get("end"))
        fallback_t0 = t0 if t0 is not None else 0.0
        fallback_t1 = t1 if t1 is not None else fallback_t0
        words: list[TranscriptWord] = []
        for item in words_raw:
            mapped = _word(item, fallback_t0=fallback_t0, fallback_t1=fallback_t1)
            if mapped is not None:
                words.append(mapped)
        text = _coerce_text(raw.get("transcript"))
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
                lang=_coerce_text(raw.get("language")) or language,
                text=text,
                words=words,
            )
        )
    return Transcript(
        provider="deepgram",
        model=model,
        prompt_version=prompt_version,
        language=language,
        segments=segments,
    )


def duration_s_from_deepgram(payload: dict) -> float:
    if not isinstance(payload, dict):
        return 0.0
    meta = payload.get("metadata")
    if isinstance(meta, dict):
        duration = _finite_float(meta.get("duration"))
        if duration is not None:
            return max(duration, 0.0)
    results = payload.get("results")
    if isinstance(results, dict):
        utterances = [item for item in (results.get("utterances") or []) if isinstance(item, dict)]
        if utterances:
            end = _finite_float(utterances[-1].get("end"))
            if end is not None:
                return max(end, 0.0)
    return 0.0


class DeepgramAsrProvider:
    """Hosted Deepgram Nova-3. Uploads audio only. Opt-in via TVA_ASR_PROVIDER=deepgram."""

    name = "deepgram"
    model = DEFAULT_DEEPGRAM_MODEL

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client
        self.last_cost_usd: float | None = None

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript:
        self.last_cost_usd = None
        key = (os.environ.get(ENV_DEEPGRAM_KEY) or "").strip()
        if not key:
            raise AsrError("DEEPGRAM_API_KEY is unset. Set it or use TVA_ASR_PROVIDER=fake.")
        if not audio.is_file():
            raise AsrError(f"audio not found: {audio}")
        content_type = _content_type(audio)
        language = language or "de"
        model_name = (os.environ.get("TVA_ASR_MODEL") or self.model).strip() or self.model
        keyterms = glossary_keyterms()
        prompt_version = f"jargon-v1+{model_name}+deepgram"
        params: list[tuple[str, str]] = [
            ("model", model_name),
            ("language", language),
            ("smart_format", "true"),
            ("punctuate", "true"),
            ("utterances", "true"),
        ]
        for term in keyterms:
            params.append(("keyterm", term))

        headers = {
            "Authorization": f"Token {key}",
            "Content-Type": content_type,
        }
        body = audio.read_bytes()
        payload = self._post(params=params, headers=headers, content=body)
        try:
            transcript = transcript_from_deepgram(
                payload,
                language=language,
                model=model_name,
                prompt_version=prompt_version,
            )
        except AsrError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise AsrError(f"Deepgram response could not be mapped: {exc}") from exc
        self.last_cost_usd = estimate_cost_usd(duration_s_from_deepgram(payload))
        return transcript

    def _post(
        self,
        *,
        params: list[tuple[str, str]],
        headers: dict[str, str],
        content: bytes,
    ) -> dict[str, Any]:
        try:
            if self._client is not None:
                response = self._client.post(
                    DEEPGRAM_LISTEN_URL, params=params, headers=headers, content=content
                )
            else:
                with httpx.Client(timeout=300.0) as client:
                    response = client.post(
                        DEEPGRAM_LISTEN_URL, params=params, headers=headers, content=content
                    )
        except httpx.HTTPError as exc:
            raise AsrError(f"Deepgram request failed: {exc}") from exc
        if response.status_code >= 400:
            snippet = response.text[:300]
            raise AsrError(f"Deepgram HTTP {response.status_code}: {snippet}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AsrError("Deepgram returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise AsrError("Deepgram returned a non-object JSON body")
        return payload


class ScribeAsrProvider:
    """Reserved ElevenLabs Scribe interface. PR-05 pick is Deepgram."""

    name = "scribe"
    model = "scribe_v2"
    last_cost_usd: float | None = None

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript:
        raise AsrError(
            "Scribe is not the PR-05 hosted pick. Use TVA_ASR_PROVIDER=deepgram "
            f"(ELEVENLABS_API_KEY is reserved). audio={audio} language={language}"
        )
