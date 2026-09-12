"""Insight extractors. Fake is a cited keyword scan — never invents quotes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any, Protocol

import httpx

from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.schema import CitedSpan, Insights, Transcript, TranscriptSegment
from tradevidanalyser.wer import contains_token

_BIAS = re.compile(r"\b(bias|richtung|long|short|bullish|bearish)\b", re.IGNORECASE)
_PLAYBOOK = re.compile(r"\b(playbook|setup|skalp|scalp|swing)\b", re.IGNORECASE)
_STOP = re.compile(r"\b(stop|ziel|target|invalid)\b", re.IGNORECASE)
_CHECKIN = re.compile(r"\b(check-?in|check in|stundencheck)\b", re.IGNORECASE)
_TILT = re.compile(r"\b(tilt|tilted|rache|revenge)\b", re.IGNORECASE)
_BRIEF = re.compile(r"\b(brief|briefing|grok)\b", re.IGNORECASE)

WINDOW_SEGMENTS = 40
WINDOW_OVERLAP = 5
DEFAULT_GROK_MODEL = "grok-4.6"
ENV_XAI_KEY = "XAI_API_KEY"
ENV_EXTRACT_MODEL = "TVA_EXTRACT_MODEL"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
# grok-4.6 is a reasoning model; xAI documents a 3600s client timeout.
HTTP_TIMEOUT = 3600.0
PROMPT_FILENAME = "insights_v1.de.md"
INSIGHT_SPAN_FIELDS = (
    "bias_statements",
    "playbooks_mentioned",
    "stated_levels",
    "stated_stops_targets",
    "checkins",
    "tilt_markers",
    "brief_refs",
    "observations",
)


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
        tokens = load_glossary().level_tokens
        levels: list[CitedSpan] = []
        for seg in transcript.segments:
            for token in tokens:
                if contains_token(seg.text, token):
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


def _walk_prompt_candidates(start: Path, *, levels: int) -> list[Path]:
    """Walk *start* and its parents for prompts/insights_v1.de.md."""
    found: list[Path] = []
    current = start
    for _ in range(levels):
        found.append(current / "prompts" / PROMPT_FILENAME)
        if current.parent == current:
            break
        current = current.parent
    return found


def default_prompt_path() -> Path:
    """Resolve the repo prompt from this file, then cwd.

    ``extract.py`` lives one directory deeper than ``glossary.py``, so
    ``Path(__file__).parents[2]`` is ``src/`` and misses ``prompts/``.
    Walk parents of ``__file__`` first so ``tva extract`` works when cwd
    is TVA_ROOT on the NAS.
    """
    here = Path(__file__).resolve()
    candidates = _walk_prompt_candidates(here.parent, levels=8)
    candidates.extend(_walk_prompt_candidates(Path.cwd(), levels=6))
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    raise ExtractError(f"prompts/{PROMPT_FILENAME} not found")


def prompt_version_for(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{path.stem}+{digest[:12]}"


def insights_json_schema() -> dict[str, Any]:
    """JSON Schema for constrained Grok output, from the Pydantic Insights model."""
    return Insights.model_json_schema()


def window_segments(
    segments: list[Any],
    *,
    size: int = WINDOW_SEGMENTS,
    overlap: int = WINDOW_OVERLAP,
) -> list[list[Any]]:
    if size <= 0:
        raise ExtractError("window size must be >= 1")
    if not segments:
        return []
    step = max(size - max(overlap, 0), 1)
    windows: list[list[Any]] = []
    index = 0
    n = len(segments)
    while index < n:
        windows.append(list(segments[index : index + size]))
        if index + size >= n:
            break
        index += step
    return windows


def _span_rank(span: CitedSpan) -> tuple[int, int, int, int, str, str, str, str]:
    """Order-independent pick: longer (more complete) quote, then lexicographic."""
    text = span.text or ""
    name = span.name or ""
    token = span.token or ""
    raw = span.raw_text or ""
    return (-len(text), -len(name), -len(token), -len(raw), text, name, token, raw)


def merge_partial_insights(
    parts: list[Insights],
    *,
    provider: str,
    model: str,
    prompt_version: str,
) -> Insights:
    """De-duplicate window results by (seg, field). Order-independent."""
    chosen: dict[str, dict[str, CitedSpan]] = {field: {} for field in INSIGHT_SPAN_FIELDS}
    gaps: list[str] = []
    for part in parts:
        for field in INSIGHT_SPAN_FIELDS:
            bucket = chosen[field]
            for span in getattr(part, field):
                existing = bucket.get(span.seg)
                if existing is None or _span_rank(span) < _span_rank(existing):
                    bucket[span.seg] = span
        gaps.extend(part.gaps)
    merged: dict[str, list[CitedSpan]] = {}
    for field, bucket in chosen.items():
        merged[field] = [bucket[seg] for seg in sorted(bucket)]
    unique_gaps = sorted(set(gaps))
    return Insights(
        provider=provider,
        model=model,
        prompt_version=prompt_version,
        gaps=unique_gaps,
        **merged,
    )


def _nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def _quote_in_segment(segment_text: str, quote: str) -> bool:
    """Verbatim substring after NFC so German umlauts (ä vs a + combining) match."""
    if not quote:
        return True
    return _nfc(quote) in _nfc(segment_text)


def citation_problems(transcript: Transcript, insights: Insights) -> list[tuple[str, CitedSpan, str]]:
    """Shared citation guard. pipeline._assert_citations raises; Grok drops into gaps."""
    known = {seg.id: seg.text for seg in transcript.segments}
    problems: list[tuple[str, CitedSpan, str]] = []
    for field in INSIGHT_SPAN_FIELDS:
        for span in getattr(insights, field):
            if span.seg not in known:
                problems.append((field, span, f"citation {span.seg} is not in the transcript"))
                continue
            segment_text = known[span.seg]
            if span.text and not _quote_in_segment(segment_text, span.text):
                problems.append((field, span, f"quote not found in {span.seg}"))
            elif span.raw_text and not _quote_in_segment(segment_text, span.raw_text):
                problems.append((field, span, f"raw_text not found in {span.seg}"))
            elif span.token and not (
                contains_token(segment_text, span.token)
                or _quote_in_segment(segment_text, span.token)
            ):
                problems.append((field, span, f"token not found in {span.seg}"))
    return problems


def apply_citation_guard(transcript: Transcript, insights: Insights) -> Insights:
    """Drop offending spans into gaps[] with a reason instead of failing the stage."""
    problems = citation_problems(transcript, insights)
    if not problems:
        return insights
    drop: dict[str, set[int]] = {field: set() for field in INSIGHT_SPAN_FIELDS}
    gaps = list(insights.gaps)
    for field, span, reason in problems:
        drop[field].add(id(span))
        gaps.append(f"dropped {field} {span.seg}: {reason}")
    cleaned: dict[str, list[CitedSpan]] = {}
    for field in INSIGHT_SPAN_FIELDS:
        marked = drop[field]
        cleaned[field] = [span for span in getattr(insights, field) if id(span) not in marked]
    unique_gaps = sorted(set(gaps))
    return insights.model_copy(update={**cleaned, "gaps": unique_gaps})


def _window_transcript(transcript: Transcript, window: list[Any]) -> Transcript:
    """Citation-check a window reply against only the segments that window saw."""
    segments = [seg for seg in window if isinstance(seg, TranscriptSegment)]
    return Transcript(
        provider=transcript.provider,
        model=transcript.model,
        language=transcript.language,
        prompt_version=transcript.prompt_version,
        segments=segments,
    )


def _format_window(segments: list[Any]) -> str:
    lines = [
        "Transkriptfenster. Zitiere ausschließlich diese seg-ids und nur wörtliche Teilstrings:",
        "",
    ]
    for seg in segments:
        lines.append(f"[{seg.id} t={seg.t0:.2f}–{seg.t1:.2f}] {seg.text}")
    return "\n".join(lines)


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def insights_from_chat_payload(payload: dict[str, Any]) -> Insights:
    if not isinstance(payload, dict):
        raise ExtractError("Grok returned a non-object JSON body")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ExtractError("Grok response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ExtractError("Grok choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ExtractError("Grok choice has no message")
    content = message.get("content")
    if isinstance(content, dict):
        body: Any = content
    else:
        raw = _message_content(message).strip()
        if not raw:
            refusal = message.get("refusal")
            if refusal:
                raise ExtractError(f"Grok refused: {refusal}")
            raise ExtractError("Grok message content is empty")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ExtractError("Grok message content is not JSON") from exc
    if not isinstance(body, dict):
        raise ExtractError("Grok JSON content is not an object")
    body.setdefault("provider", "grok")
    body.setdefault("model", "unknown")
    try:
        return Insights.model_validate(body)
    except (TypeError, ValueError) as exc:
        raise ExtractError(f"Grok JSON did not match Insights: {exc}") from exc


class GrokExtractProvider:
    """xAI Grok extractor. Opt-in via TVA_EXTRACT_PROVIDER=grok. Fake stays default."""

    name = "grok"
    model = DEFAULT_GROK_MODEL

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        window_size: int = WINDOW_SEGMENTS,
        overlap: int = WINDOW_OVERLAP,
        prompt_path: Path | None = None,
    ) -> None:
        self._client = client
        self.window_size = window_size
        self.overlap = overlap
        self.prompt_path = prompt_path
        env_model = (os.environ.get(ENV_EXTRACT_MODEL) or "").strip()
        if env_model:
            self.model = env_model

    def extract(self, transcript: Transcript) -> Insights:
        key = (os.environ.get(ENV_XAI_KEY) or "").strip()
        if not key:
            raise ExtractError("XAI_API_KEY is unset. Set it or use TVA_EXTRACT_PROVIDER=fake.")
        path = self.prompt_path or default_prompt_path()
        system_prompt = path.read_text(encoding="utf-8")
        version = prompt_version_for(path)
        windows = window_segments(
            list(transcript.segments),
            size=self.window_size,
            overlap=self.overlap,
        )
        if not windows:
            return Insights(
                provider=self.name,
                model=self.model,
                prompt_version=version,
                gaps=["empty transcript"],
            )
        schema = insights_json_schema()
        parts: list[Insights] = []
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            for window in windows:
                payload = self._complete(
                    client,
                    key=key,
                    system_prompt=system_prompt,
                    user_prompt=_format_window(window),
                    schema=schema,
                )
                part = insights_from_chat_payload(payload)
                parts.append(apply_citation_guard(_window_transcript(transcript, window), part))
        finally:
            if owns_client:
                client.close()
        merged = merge_partial_insights(
            parts,
            provider=self.name,
            model=self.model,
            prompt_version=version,
        )
        return apply_citation_guard(transcript, merged)

    def _complete(
        self,
        client: httpx.Client,
        *,
        key: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "insights",
                    "strict": True,
                    "schema": schema,
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        try:
            response = client.post(
                XAI_CHAT_URL, json=body, headers=headers, timeout=HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise ExtractError(f"Grok request failed: {exc}") from exc
        if response.status_code >= 400:
            raise ExtractError(f"Grok HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExtractError("Grok returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ExtractError("Grok returned a non-object JSON body")
        return payload


def get_extract_provider(name: str | None = None) -> ExtractProvider:
    chosen = (name or os.environ.get("TVA_EXTRACT_PROVIDER") or "fake").strip().lower()
    if chosen in {"fake", "test", "keyword"}:
        return FakeExtractProvider()
    if chosen in {"grok", "xai"}:
        return GrokExtractProvider()
    raise ExtractError(f"unknown extract provider {chosen!r}")
