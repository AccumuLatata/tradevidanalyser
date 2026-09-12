"""ASR adapters. Default is fake so CI and first-run never call a paid API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from tradevidanalyser.schema import Transcript, TranscriptSegment, TranscriptWord


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


class WhisperXAsrProvider:
    name = "whisperx"
    model = "large-v3"

    def transcribe(self, audio: Path, *, language: str = "de") -> Transcript:
        raise AsrError(
            "WhisperX adapter is not wired yet. Set TVA_ASR_PROVIDER=fake "
            "or wait for TVA1. Audio was: " + str(audio)
        )


def get_asr_provider(name: str | None = None) -> AsrProvider:
    chosen = (name or os.environ.get("TVA_ASR_PROVIDER") or "fake").strip().lower()
    if chosen in {"fake", "test"}:
        return FakeAsrProvider()
    if chosen in {"whisperx", "whisper"}:
        return WhisperXAsrProvider()
    raise AsrError(f"unknown ASR provider {chosen!r}")
