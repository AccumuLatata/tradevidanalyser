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
from pydantic import BaseModel, Field

from tradevidanalyser.schema import (
    EVENT_KINDS,
    CitedSpan,
    EventKind,
    Insights,
    SessionEvent,
    Transcript,
    TranscriptSegment,
)
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
EVENTS_PROMPT_FILENAME = "events_v1.de.md"
SUMMARY_WORD_LIMIT = 120
_DIGIT_RUN = re.compile(r"\d+")
_WINDOW_EXCLUDE = frozenset({"session_events", "summary_de", "summary_en"})
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

        bias = _hits(_BIAS, transcript)
        checkins = _hits(_CHECKIN, transcript)
        tilts = _hits(_TILT, transcript)
        briefs = _hits(_BRIEF, transcript)
        events: list[SessionEvent] = []
        for span, kind in (
            *((item, "bias_statement") for item in bias),
            *((item, "hourly_checkin") for item in checkins),
            *((item, "tilt") for item in tilts),
            *((item, "rule_mention") for item in playbooks),
        ):
            events.append(
                SessionEvent(t=span.t or 0.0, kind=kind, seg=span.seg, text=span.text)
            )
        for span in briefs:
            kind: EventKind = "grok_ref" if re.search(r"\bgrok\b", span.text, re.I) else "brief_ref"
            events.append(SessionEvent(t=span.t or 0.0, kind=kind, seg=span.seg, text=span.text))

        return Insights(
            provider=self.name,
            model=self.model,
            bias_statements=bias,
            playbooks_mentioned=playbooks,
            stated_levels=levels,
            stated_stops_targets=stops,
            checkins=checkins,
            tilt_markers=tilts,
            brief_refs=briefs,
            observations=[],
            gaps=gaps,
            session_events=events,
        )


def _walk_prompt_candidates(start: Path, filename: str, *, levels: int) -> list[Path]:
    """Walk *start* and its parents for prompts/<filename>."""
    found: list[Path] = []
    current = start
    for _ in range(levels):
        found.append(current / "prompts" / filename)
        if current.parent == current:
            break
        current = current.parent
    return found


def default_prompt_path(filename: str = PROMPT_FILENAME) -> Path:
    """Resolve a repo prompt from this file, then cwd.

    ``extract.py`` lives one directory deeper than ``glossary.py``, so
    ``Path(__file__).parents[2]`` is ``src/`` and misses ``prompts/``.
    Walk parents of ``__file__`` first so ``tva extract`` works when cwd
    is TVA_ROOT on the NAS.
    """
    here = Path(__file__).resolve()
    candidates = _walk_prompt_candidates(here.parent, filename, levels=8)
    candidates.extend(_walk_prompt_candidates(Path.cwd(), filename, levels=6))
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    raise ExtractError(f"prompts/{filename} not found")


def prompt_version_for(path: Path) -> str:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return f"{path.stem}+{digest[:12]}"


def insights_json_schema() -> dict[str, Any]:
    """Window-pass schema: Insights minus PR-09 event/summary fields."""
    schema = Insights.model_json_schema()
    props = schema.get("properties")
    if isinstance(props, dict):
        for key in _WINDOW_EXCLUDE:
            props.pop(key, None)
    required = schema.get("required")
    if isinstance(required, list):
        schema["required"] = [key for key in required if key not in _WINDOW_EXCLUDE]
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        defs.pop("SessionEvent", None)
    return schema


class SessionEventDraft(BaseModel):
    """Second-pass event: ids only. Text is re-hydrated from the transcript."""

    kind: str
    seg: str
    t: float | None = None


class EventsPass(BaseModel):
    session_events: list[SessionEventDraft] = Field(default_factory=list)
    summary_de: str = ""
    summary_en: str = ""


def events_pass_json_schema() -> dict[str, Any]:
    return EventsPass.model_json_schema()


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


def _cited_segment_blob(transcript: Transcript, insights: Insights) -> str:
    known = {seg.id: seg.text for seg in transcript.segments}
    segs: list[str] = []
    seen: set[str] = set()
    for field in INSIGHT_SPAN_FIELDS:
        for span in getattr(insights, field):
            if span.seg not in seen:
                seen.add(span.seg)
                segs.append(span.seg)
    for event in insights.session_events:
        if event.seg not in seen:
            seen.add(event.seg)
            segs.append(event.seg)
    return " ".join(known[seg] for seg in segs if seg in known)


def citation_problems(
    transcript: Transcript, insights: Insights
) -> list[tuple[str, CitedSpan | SessionEvent | str, str]]:
    """Shared citation guard. pipeline._assert_citations raises; Grok drops into gaps."""
    known = {seg.id: seg.text for seg in transcript.segments}
    problems: list[tuple[str, CitedSpan | SessionEvent | str, str]] = []
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
    for event in insights.session_events:
        if event.kind not in EVENT_KINDS:
            problems.append(
                ("session_events", event, f"unknown kind {event.kind!r}")
            )
            continue
        if event.seg not in known:
            problems.append(
                ("session_events", event, f"citation {event.seg} is not in the transcript")
            )
            continue
        if event.text and not _quote_in_segment(known[event.seg], event.text):
            problems.append(("session_events", event, f"quote not found in {event.seg}"))
    cited = _nfc(_cited_segment_blob(transcript, insights))
    for field in ("summary_de", "summary_en"):
        text = getattr(insights, field) or ""
        if not text:
            continue
        if len(text.split()) > SUMMARY_WORD_LIMIT:
            problems.append((field, text, f"{field} exceeds {SUMMARY_WORD_LIMIT} words"))
        for run in _DIGIT_RUN.findall(text):
            if run not in cited:
                problems.append(
                    (field, text, f"{field} digit {run} not in cited segments")
                )
    return problems


def apply_citation_guard(transcript: Transcript, insights: Insights) -> Insights:
    """Drop offending spans/events and leaky summaries into gaps[] instead of failing."""
    problems = citation_problems(transcript, insights)
    if not problems:
        return insights
    drop: dict[str, set[int]] = {field: set() for field in INSIGHT_SPAN_FIELDS}
    drop_events: set[int] = set()
    clear_summaries: set[str] = set()
    gaps = list(insights.gaps)
    for field, obj, reason in problems:
        if field in INSIGHT_SPAN_FIELDS and isinstance(obj, CitedSpan):
            drop[field].add(id(obj))
            gaps.append(f"dropped {field} {obj.seg}: {reason}")
        elif field == "session_events" and isinstance(obj, SessionEvent):
            drop_events.add(id(obj))
            gaps.append(f"dropped session_events {obj.seg}: {reason}")
        elif field in {"summary_de", "summary_en"}:
            clear_summaries.add(field)
            gaps.append(f"dropped {field}: {reason}")
    cleaned: dict[str, Any] = {}
    for field in INSIGHT_SPAN_FIELDS:
        marked = drop[field]
        cleaned[field] = [span for span in getattr(insights, field) if id(span) not in marked]
    cleaned["session_events"] = [
        event for event in insights.session_events if id(event) not in drop_events
    ]
    for field in clear_summaries:
        cleaned[field] = ""
    cleaned["gaps"] = sorted(set(gaps))
    return insights.model_copy(update=cleaned)


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


def _format_timeline_ids(segments: list[Any]) -> str:
    lines = [
        "Zeitleiste. Nur seg-ids und Zeiten — keinen Segmenttext. Text setzt der Client.",
        "",
    ]
    for seg in segments:
        lines.append(f"{seg.id} t={seg.t0:.2f}–{seg.t1:.2f}")
    return "\n".join(lines)


def hydrate_session_events(
    drafts: list[SessionEventDraft],
    transcript: Transcript,
) -> tuple[list[SessionEvent], list[str]]:
    """Fill event.text from the transcript. Drop unknown segs or kinds."""
    known = {seg.id: seg for seg in transcript.segments}
    events: list[SessionEvent] = []
    gaps: list[str] = []
    seen: set[tuple[str, str]] = set()
    for draft in drafts:
        if draft.kind not in EVENT_KINDS:
            gaps.append(f"dropped session_events {draft.seg}: unknown kind {draft.kind!r}")
            continue
        segment = known.get(draft.seg)
        if segment is None:
            gaps.append(
                f"dropped session_events {draft.seg}: citation {draft.seg} is not in the transcript"
            )
            continue
        key = (draft.kind, draft.seg)
        if key in seen:
            continue
        seen.add(key)
        events.append(
            SessionEvent(t=segment.t0, kind=draft.kind, seg=draft.seg, text=segment.text)
        )
    events.sort(key=lambda item: (item.t, item.seg, item.kind))
    return events, gaps


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


def _payload_body(payload: dict[str, Any]) -> dict[str, Any]:
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
    return body


def events_from_chat_payload(payload: dict[str, Any]) -> EventsPass:
    body = _payload_body(payload)
    try:
        return EventsPass.model_validate(body)
    except (TypeError, ValueError) as exc:
        raise ExtractError(f"Grok JSON did not match EventsPass: {exc}") from exc


def insights_from_chat_payload(payload: dict[str, Any]) -> Insights:
    body = _payload_body(payload)
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
        events_path = default_prompt_path(EVENTS_PROMPT_FILENAME)
        events_prompt = events_path.read_text(encoding="utf-8")
        version = f"{version}+{prompt_version_for(events_path)}"
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
                    schema_name="insights",
                )
                part = insights_from_chat_payload(payload)
                parts.append(apply_citation_guard(_window_transcript(transcript, window), part))
            merged = merge_partial_insights(
                parts,
                provider=self.name,
                model=self.model,
                prompt_version=version,
            )
            events_payload = self._complete(
                client,
                key=key,
                system_prompt=events_prompt,
                user_prompt=_format_timeline_ids(list(transcript.segments)),
                schema=events_pass_json_schema(),
                schema_name="session_events",
            )
            draft = events_from_chat_payload(events_payload)
            events, event_gaps = hydrate_session_events(draft.session_events, transcript)
            merged = merged.model_copy(
                update={
                    "session_events": events,
                    "summary_de": draft.summary_de,
                    "summary_en": draft.summary_en,
                    "gaps": sorted(set(merged.gaps) | set(event_gaps)),
                }
            )
        finally:
            if owns_client:
                client.close()
        return apply_citation_guard(transcript, merged)

    def _complete(
        self,
        client: httpx.Client,
        *,
        key: str,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
        schema_name: str = "insights",
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
                    "name": schema_name,
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
