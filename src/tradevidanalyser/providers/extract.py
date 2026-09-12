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

from pydantic import BaseModel, Field

from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.schema import (
    EVENT_KINDS,
    CitedSpan,
    Evidence,
    Insights,
    SessionEvent,
    StatedCite,
    StatedFields,
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
STATED_PROMPT_FILENAME = "stated_v1.de.md"
STATED_PROMPT_VERSION_FAKE = "stated-keyword-v1"
STATED_FIELD_NAMES = ("setup", "bias", "stop_raw", "target_raw", "playbook")
NO_SPEECH_GAP = "no speech in window"
SUMMARY_WORD_LIMIT = 120
_DIGIT_RUN = re.compile(r"\d+")
_WINDOW_EXCLUDE = frozenset(
    {"session_events", "summary_de", "summary_en", "visual_notes"}
)
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


class StatedFieldsPass(BaseModel):
    """Per-window stated fields. Gaps stay on the pass, not on StatedFields."""

    setup: StatedCite | None = None
    bias: StatedCite | None = None
    stop_raw: StatedCite | None = None
    target_raw: StatedCite | None = None
    playbook: StatedCite | None = None
    gaps: list[str] = Field(default_factory=list)


class ExtractProvider(Protocol):
    name: str
    model: str

    def extract(self, transcript: Transcript) -> Insights: ...

    def stated_fields(self, transcript: Transcript) -> StatedFieldsPass: ...


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
            if re.search(r"\bgrok\b", span.text, re.I):
                events.append(
                    SessionEvent(t=span.t or 0.0, kind="grok_ref", seg=span.seg, text=span.text)
                )
            if re.search(r"\b(brief|briefing)\b", span.text, re.I):
                events.append(
                    SessionEvent(t=span.t or 0.0, kind="brief_ref", seg=span.seg, text=span.text)
                )

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

    def stated_fields(self, transcript: Transcript) -> StatedFieldsPass:
        return _fake_stated_fields(transcript)


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
        defs.pop("VisualNote", None)
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
    """Second-pass schema: closed kind set, no extra properties (no event text)."""
    schema = EventsPass.model_json_schema()
    schema["additionalProperties"] = False
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        draft = defs.get("SessionEventDraft")
        if isinstance(draft, dict):
            props = draft.get("properties")
            if isinstance(props, dict) and "kind" in props:
                props["kind"] = {"type": "string", "enum": list(EVENT_KINDS)}
            draft["additionalProperties"] = False
    return schema


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


def _span_problem(span: CitedSpan, known: dict[str, str]) -> str | None:
    if span.seg not in known:
        return f"citation {span.seg} is not in the transcript"
    segment_text = known[span.seg]
    if span.text and not _quote_in_segment(segment_text, span.text):
        return f"quote not found in {span.seg}"
    if span.raw_text and not _quote_in_segment(segment_text, span.raw_text):
        return f"raw_text not found in {span.seg}"
    if span.token and not (
        contains_token(segment_text, span.token) or _quote_in_segment(segment_text, span.token)
    ):
        return f"token not found in {span.seg}"
    return None


def _event_problem(event: SessionEvent, known: dict[str, str]) -> str | None:
    if event.kind not in EVENT_KINDS:
        return f"unknown kind {event.kind!r}"
    if event.seg not in known:
        return f"citation {event.seg} is not in the transcript"
    if event.text and not _quote_in_segment(known[event.seg], event.text):
        return f"quote not found in {event.seg}"
    return None


def _cited_segment_blob(transcript: Transcript, insights: Insights) -> str:
    """Concatenate text of segments that still have a valid citation or event."""
    known = {seg.id: seg.text for seg in transcript.segments}
    segs: list[str] = []
    seen: set[str] = set()
    for field in INSIGHT_SPAN_FIELDS:
        for span in getattr(insights, field):
            if _span_problem(span, known) is not None:
                continue
            if not (span.text or span.raw_text or span.token):
                continue
            if span.seg not in seen:
                seen.add(span.seg)
                segs.append(span.seg)
    for event in insights.session_events:
        if _event_problem(event, known) is not None:
            continue
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
            reason = _span_problem(span, known)
            if reason:
                problems.append((field, span, reason))
    for event in insights.session_events:
        reason = _event_problem(event, known)
        if reason:
            problems.append(("session_events", event, reason))
    cited_runs = set(_DIGIT_RUN.findall(_nfc(_cited_segment_blob(transcript, insights))))
    for field in ("summary_de", "summary_en"):
        text = getattr(insights, field) or ""
        if not text:
            continue
        if field == "summary_de" and len(text.split()) > SUMMARY_WORD_LIMIT:
            problems.append((field, text, f"{field} exceeds {SUMMARY_WORD_LIMIT} words"))
        for run in _DIGIT_RUN.findall(text):
            if run not in cited_runs:
                problems.append((field, text, f"{field} digit {run} not in cited segments"))
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


_SETUP_PHRASE = re.compile(r"\b(ONH Touch Scalp|ONH Touch|setup|skalp|scalp|swing)\b", re.IGNORECASE)
_BIAS_PHRASE = re.compile(
    r"\b(bias ist long|bias ist short|bias long|bias short|kein trade|long|short|bullish|bearish)\b",
    re.IGNORECASE,
)
_STOP_PHRASE = re.compile(r"\b(stop unter[^.]{0,40}|stop [^.]{0,40}|invalid[^.]{0,20})\b", re.IGNORECASE)
_TARGET_PHRASE = re.compile(r"\b(ziel am [^.]{0,40}|ziel [^.]{0,40}|target [^.]{0,40})\b", re.IGNORECASE)
_PLAYBOOK_PHRASE = re.compile(
    r"\b(ONH Touch Scalp|ONH Touch|playbook [^.]+|playbook)\b",
    re.IGNORECASE,
)


def _first_stated(pattern: re.Pattern[str], transcript: Transcript) -> StatedCite | None:
    for seg in transcript.segments:
        match = pattern.search(seg.text or "")
        if not match:
            continue
        value = match.group(0).strip()
        if value and _quote_in_segment(seg.text, value):
            return StatedCite(value=value, seg=seg.id)
    return None


def _has_speech(transcript: Transcript) -> bool:
    return any((seg.text or "").strip() for seg in transcript.segments)


def _fake_stated_fields(transcript: Transcript) -> StatedFieldsPass:
    if not _has_speech(transcript):
        return StatedFieldsPass(gaps=[NO_SPEECH_GAP])
    return StatedFieldsPass(
        setup=_first_stated(_SETUP_PHRASE, transcript),
        bias=_first_stated(_BIAS_PHRASE, transcript),
        stop_raw=_first_stated(_STOP_PHRASE, transcript),
        target_raw=_first_stated(_TARGET_PHRASE, transcript),
        playbook=_first_stated(_PLAYBOOK_PHRASE, transcript),
    )


def _stated_cite_problem(cite: StatedCite, known: dict[str, str]) -> str | None:
    if cite.seg not in known:
        return f"citation {cite.seg} is not in the transcript"
    if not cite.value or not _quote_in_segment(known[cite.seg], cite.value):
        return f"quote not found in {cite.seg}"
    return None


def evidence_citation_problems(
    transcript: Transcript, evidence: Evidence
) -> list[tuple[str, StatedCite | str, str]]:
    """Citation guard for evidence.json. pipeline._assert_citations raises."""
    known = {seg.id: seg.text for seg in transcript.segments}
    problems: list[tuple[str, StatedCite | str, str]] = []
    for trade in evidence.trades:
        prefix = trade.tva_trade_id
        for seg_id in trade.commentary:
            if seg_id not in known:
                problems.append(
                    (f"{prefix}.commentary", seg_id, f"citation {seg_id} is not in the transcript")
                )
        for field in STATED_FIELD_NAMES:
            cite = getattr(trade.stated, field)
            if cite is None:
                continue
            reason = _stated_cite_problem(cite, known)
            if reason:
                problems.append((f"{prefix}.stated.{field}", cite, reason))
    return problems


def apply_stated_citation_guard(
    transcript: Transcript, stated: StatedFieldsPass
) -> StatedFieldsPass:
    """Null fabricated stated fields and leak them into gaps[]."""
    known = {seg.id: seg.text for seg in transcript.segments}
    gaps = list(stated.gaps)
    cleaned: dict[str, StatedCite | None] = {}
    for field in STATED_FIELD_NAMES:
        cite = getattr(stated, field)
        if cite is None:
            cleaned[field] = None
            continue
        reason = _stated_cite_problem(cite, known)
        if reason:
            cleaned[field] = None
            gaps.append(f"dropped {field} {cite.seg}: {reason}")
        else:
            cleaned[field] = cite
    cleaned["gaps"] = sorted(set(gaps))
    return stated.model_copy(update=cleaned)


def stated_fields_of(stated: StatedFieldsPass) -> StatedFields:
    return StatedFields(
        setup=stated.setup,
        bias=stated.bias,
        stop_raw=stated.stop_raw,
        target_raw=stated.target_raw,
        playbook=stated.playbook,
    )


def stated_fields_json_schema() -> dict[str, Any]:
    schema = StatedFieldsPass.model_json_schema()
    schema["additionalProperties"] = False
    defs = schema.get("$defs")
    if isinstance(defs, dict):
        cite = defs.get("StatedCite")
        if isinstance(cite, dict):
            cite["additionalProperties"] = False
    return schema


def stated_from_chat_payload(payload: dict[str, Any]) -> StatedFieldsPass:
    body = _payload_body(payload)
    try:
        return StatedFieldsPass.model_validate(body)
    except (TypeError, ValueError) as exc:
        raise ExtractError(f"Grok JSON did not match StatedFieldsPass: {exc}") from exc


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


def _format_timeline_ids(
    segments: list[Any], insights: Insights | None = None
) -> str:
    lines = [
        "Zeitleiste. Nur seg-ids und Zeiten — keinen Segmenttext. Text setzt der Client.",
        "",
    ]
    for seg in segments:
        lines.append(f"{seg.id} t={seg.t0:.2f}–{seg.t1:.2f}")
    if insights is not None:
        cited_lines = []
        for field in INSIGHT_SPAN_FIELDS:
            segs = sorted({span.seg for span in getattr(insights, field)})
            if segs:
                cited_lines.append(f"{field}: {', '.join(segs)}")
        if cited_lines:
            lines.append("")
            lines.append("Fenster-Fundstellen (nur seg-ids, kein Text):")
            lines.extend(cited_lines)
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
    for key in _WINDOW_EXCLUDE:
        body.pop(key, None)
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
        events_path = default_prompt_path(EVENTS_PROMPT_FILENAME)
        events_prompt = events_path.read_text(encoding="utf-8")
        version = f"{prompt_version_for(path)}+{prompt_version_for(events_path)}"
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
                user_prompt=_format_timeline_ids(list(transcript.segments), merged),
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

    def stated_fields(self, transcript: Transcript) -> StatedFieldsPass:
        key = (os.environ.get(ENV_XAI_KEY) or "").strip()
        if not key:
            raise ExtractError("XAI_API_KEY is unset. Set it or use TVA_EXTRACT_PROVIDER=fake.")
        path = default_prompt_path(STATED_PROMPT_FILENAME)
        if not _has_speech(transcript):
            return StatedFieldsPass(gaps=[NO_SPEECH_GAP])
        owns_client = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            payload = self._complete(
                client,
                key=key,
                system_prompt=path.read_text(encoding="utf-8"),
                user_prompt=_format_window(list(transcript.segments)),
                schema=stated_fields_json_schema(),
                schema_name="stated_fields",
            )
        finally:
            if owns_client:
                client.close()
        return apply_stated_citation_guard(transcript, stated_from_chat_payload(payload))

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
