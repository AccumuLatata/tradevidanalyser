"""Insight extractors. Fake is a cited keyword scan — never invents quotes."""

from __future__ import annotations

import os
import re
from typing import Protocol

from tradevidanalyser.schema import CitedSpan, Insights, Transcript

_LEVELS = (
    "ONH",
    "ONL",
    "dVWAP",
    "pwEQ",
    "pdPOC",
    "pdVAH",
    "pdVAL",
    "APOC",
    "HVL",
    "3c",
)

_BIAS = re.compile(r"\b(bias|richtung|long|short|bullish|bearish)\b", re.IGNORECASE)
_PLAYBOOK = re.compile(r"\b(playbook|setup|skalp|scalp|swing)\b", re.IGNORECASE)
_STOP = re.compile(r"\b(stop|ziel|target|invalid)\b", re.IGNORECASE)
_CHECKIN = re.compile(r"\b(check-?in|check in|stundencheck)\b", re.IGNORECASE)
_TILT = re.compile(r"\b(tilt|tilted|rache|revenge)\b", re.IGNORECASE)
_BRIEF = re.compile(r"\b(brief|briefing|grok)\b", re.IGNORECASE)


class ExtractError(RuntimeError):
    pass


class ExtractProvider(Protocol):
    name: str
    model: str

    def extract(self, transcript: Transcript) -> Insights: ...


def _hits(pattern: re.Pattern[str], transcript: Transcript) -> list[CitedSpan]:
    found: list[CitedSpan] = []
    for seg in transcript.segments:
        if pattern.search(seg.text):
            found.append(CitedSpan(seg=seg.id, text=seg.text, t=seg.t0))
    return found


class FakeExtractProvider:
    name = "fake"
    model = "keyword-v1"

    def extract(self, transcript: Transcript) -> Insights:
        levels: list[CitedSpan] = []
        for seg in transcript.segments:
            for token in _LEVELS:
                if re.search(rf"\b{re.escape(token)}\b", seg.text):
                    levels.append(CitedSpan(seg=seg.id, text=seg.text, t=seg.t0, token=token))

        playbooks: list[CitedSpan] = []
        for seg in transcript.segments:
            if _PLAYBOOK.search(seg.text):
                playbooks.append(CitedSpan(seg=seg.id, text=seg.text, t=seg.t0, name="spoken"))

        stops: list[CitedSpan] = []
        for seg in transcript.segments:
            if _STOP.search(seg.text):
                stops.append(CitedSpan(seg=seg.id, text=seg.text, t=seg.t0, raw_text=seg.text))

        gaps: list[str] = []
        if not transcript.segments:
            gaps.append("empty transcript")

        return Insights(
            provider=self.name,
            model=self.model,
            bias_statements=_hits(_BIAS, transcript),
            playbooks_mentioned=playbooks,
            stated_levels=levels,
            stated_stops_targets=stops,
            checkins=_hits(_CHECKIN, transcript),
            tilt_markers=_hits(_TILT, transcript),
            brief_refs=_hits(_BRIEF, transcript),
            observations=[],
            gaps=gaps,
        )


class GrokExtractProvider:
    name = "grok"
    model = "grok-extract"

    def extract(self, transcript: Transcript) -> Insights:
        raise ExtractError(
            "Grok extractor is not wired yet. Use TVA_EXTRACT_PROVIDER=fake. "
            f"segments={len(transcript.segments)}"
        )


def get_extract_provider(name: str | None = None) -> ExtractProvider:
    chosen = (name or os.environ.get("TVA_EXTRACT_PROVIDER") or "fake").strip().lower()
    if chosen in {"fake", "test", "keyword"}:
        return FakeExtractProvider()
    if chosen in {"grok", "xai"}:
        return GrokExtractProvider()
    raise ExtractError(f"unknown extract provider {chosen!r}")
