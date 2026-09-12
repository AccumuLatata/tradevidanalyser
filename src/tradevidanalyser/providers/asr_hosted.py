"""Hosted ASR adapters. PR-05 pick is Deepgram; Scribe keeps the same interface."""

from __future__ import annotations

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
_AUDIO_TYPES = {
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
}


def estimate_cost_usd(duration_s: float, *, usd_per_hour: float | None = None) -> float:
    rate = usd_per_hour
    if rate is None:
        raw = (os.environ.get(ENV_USD_PER_HOUR) or "").strip()
        rate = float(raw) if raw else DEEPGRAM_USD_PER_AUDIO_HOUR
    hours = max(duration_s, 0.0) / 3600.0
    return round(hours * rate, 6)


def glossary_keyterms(limit: int = MAX_KEYTERMS) -> list[str]:
    seen: list[str] = []
    for token in load_glossary().tokens:
        if token and token not in seen:
            seen.append(token)
        if len(seen) >= limit:
            break
    return seen


def _content_type(audio: Path) -> str:
    suffix = audio.suffix.lower()
    if suffix in _VIDEO_SUFFIXES:
        raise AsrError("hosted ASR uploads audio only; pass audio/mic.opus, not video")
    return _AUDIO_TYPES.get(suffix, "audio/ogg")


def _word(raw: dict) -> TranscriptWord | None:
    token = str(raw.get("punctuated_word") or raw.get("word") or "").strip()
    if not token:
        return None
    confidence = raw.get("confidence")
    return TranscriptWord(
        w=token,
        t0=float(raw.get("start") or 0.0),
        t1=float(raw.get("end") or 0.0),
        p=float(confidence if confidence is not None else 1.0),
    )


def transcript_from_deepgram(
    payload: dict,
    *,
    language: str = "de",
    model: str = DEFAULT_DEEPGRAM_MODEL,
    prompt_version: str,
) -> Transcript:
    """Map a Deepgram listen response onto Transcript with seg_NNN ids."""
    results = payload.get("results") or {}
    utterances = list(results.get("utterances") or [])
    if not utterances:
        channels = list(results.get("channels") or [])
        alt = ((channels[0].get("alternatives") or [{}])[0] if channels else {})
        text = str(alt.get("transcript") or "").strip()
        if text:
            utterances = [
                {
                    "start": (alt.get("words") or [{}])[0].get("start", 0.0) if alt.get("words") else 0.0,
                    "end": (alt.get("words") or [{}])[-1].get("end", 0.0) if alt.get("words") else 0.0,
                    "transcript": text,
                    "words": alt.get("words") or [],
                }
            ]

    segments: list[TranscriptSegment] = []
    for raw in utterances:
        text = str(raw.get("transcript") or "").strip()
        if not text:
            continue
        words: list[TranscriptWord] = []
        for item in raw.get("words") or []:
            mapped = _word(item)
            if mapped is not None:
                words.append(mapped)
        t0 = float(raw["start"]) if raw.get("start") is not None else (words[0].t0 if words else 0.0)
        t1 = float(raw["end"]) if raw.get("end") is not None else (words[-1].t1 if words else t0)
        segments.append(
            TranscriptSegment(
                id=f"seg_{len(segments) + 1:03d}",
                t0=t0,
                t1=t1,
                lang=str(raw.get("language") or language or "de"),
                text=text,
                words=words,
            )
        )
    return Transcript(
        provider="deepgram",
        model=model,
        prompt_version=prompt_version,
        language=language or "de",
        segments=segments,
    )


def duration_s_from_deepgram(payload: dict) -> float:
    meta = payload.get("metadata") or {}
    if meta.get("duration") is not None:
        return float(meta["duration"])
    results = payload.get("results") or {}
    utterances = list(results.get("utterances") or [])
    if utterances and utterances[-1].get("end") is not None:
        return float(utterances[-1]["end"])
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
        transcript = transcript_from_deepgram(
            payload,
            language=language,
            model=model_name,
            prompt_version=prompt_version,
        )
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
