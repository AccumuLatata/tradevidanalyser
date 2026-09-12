"""Session Record v1 — files on TVA_ROOT are the source of truth."""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class Chapter(BaseModel):
    t: float
    name: str = ""


class RecordingPart(BaseModel):
    path: str
    sha256: str
    duration_s: float
    offset_s: float = 0.0
    filename: str = ""


class RecordingInfo(BaseModel):
    path: str
    sha256: str
    start_wallclock_vienna: str
    duration_s: float
    tracks: list[str] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    filename: str = ""
    parts: list[RecordingPart] = Field(default_factory=list)


AlignmentMethod = Literal["ocr_clock", "filename", "chapter_fill", "manual"]


class AlignmentSample(BaseModel):
    video_t: float
    ocr_text: str
    parsed_wallclock: str
    residual_s: float


class Alignment(BaseModel):
    offset_s: float
    drift_s_per_h: float
    confidence: float
    method: AlignmentMethod
    samples: list[AlignmentSample] = Field(default_factory=list)


class SessionRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str
    recording: RecordingInfo
    language: str = "de"
    alignment: Alignment | None = None
    app_version: str = ""


class TranscriptWord(BaseModel):
    w: str
    t0: float
    t1: float
    p: float = 1.0


class TranscriptSegment(BaseModel):
    id: str
    t0: float
    t1: float
    lang: str = "de"
    text: str
    words: list[TranscriptWord] = Field(default_factory=list)


class Transcript(BaseModel):
    schema_version: str = SCHEMA_VERSION
    provider: str
    model: str
    prompt_version: str = "jargon-v1"
    language: str = "de"
    segments: list[TranscriptSegment] = Field(default_factory=list)


class CitedSpan(BaseModel):
    seg: str
    text: str
    t: float | None = None
    name: str | None = None
    token: str | None = None
    raw_text: str | None = None


EventKind = Literal[
    "hourly_checkin",
    "bias_statement",
    "no_trade_zone",
    "trade_zone",
    "tilt",
    "break",
    "rule_mention",
    "brief_ref",
    "grok_ref",
]

EVENT_KINDS: tuple[str, ...] = get_args(EventKind)


class SessionEvent(BaseModel):
    t: float
    kind: EventKind
    seg: str
    text: str = ""


class VisualNote(BaseModel):
    """Qualitative VLM note. Numbers must already appear in ocr.parquet."""

    text: str
    t: float | None = None
    frames_cited: list[str] = Field(default_factory=list)
    clip: str | None = None


class Insights(BaseModel):
    schema_version: str = SCHEMA_VERSION
    provider: str
    model: str
    prompt_version: str = "insights-v1"
    bias_statements: list[CitedSpan] = Field(default_factory=list)
    playbooks_mentioned: list[CitedSpan] = Field(default_factory=list)
    stated_levels: list[CitedSpan] = Field(default_factory=list)
    stated_stops_targets: list[CitedSpan] = Field(default_factory=list)
    checkins: list[CitedSpan] = Field(default_factory=list)
    tilt_markers: list[CitedSpan] = Field(default_factory=list)
    brief_refs: list[CitedSpan] = Field(default_factory=list)
    observations: list[CitedSpan] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    session_events: list[SessionEvent] = Field(default_factory=list)
    summary_de: str = ""
    summary_en: str = ""
    visual_notes: list[VisualNote] = Field(default_factory=list)


StageState = Literal["ok", "missing", "failed", "ingesting", "running"]


class SessionStatus(BaseModel):
    schema_version: str = SCHEMA_VERSION
    session_id: str
    stages: dict[str, StageState] = Field(default_factory=dict)
    error: str | None = None
    cost_usd: float | None = None


class DoctorCheck(BaseModel):
    id: str
    status: Literal["ok", "warn", "fail"]
    detail: str


class DoctorReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    app_version: str
    root: str
    checks: list[DoctorCheck]
    ok: bool
