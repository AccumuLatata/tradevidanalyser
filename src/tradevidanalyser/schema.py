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
    video_t: float = Field(allow_inf_nan=False)
    ocr_text: str
    parsed_wallclock: str
    residual_s: float = Field(allow_inf_nan=False)


class Alignment(BaseModel):
    offset_s: float = Field(allow_inf_nan=False)
    drift_s_per_h: float = Field(allow_inf_nan=False)
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    method: AlignmentMethod
    samples: list[AlignmentSample] = Field(default_factory=list)


class StatedCite(BaseModel):
    value: str
    seg: str


class StatedFields(BaseModel):
    setup: StatedCite | None = None
    bias: StatedCite | None = None
    stop_raw: StatedCite | None = None
    target_raw: StatedCite | None = None
    playbook: StatedCite | None = None


class EvidenceWindow(BaseModel):
    t0: float = Field(allow_inf_nan=False)
    t1: float = Field(allow_inf_nan=False)


class EvidenceOcrRef(BaseModel):
    t: float = Field(allow_inf_nan=False)
    roi: str
    text: str
    parsed: str | None = None


class EvidenceTrade(BaseModel):
    tva_trade_id: str
    window: EvidenceWindow
    commentary: list[str] = Field(default_factory=list)
    stated: StatedFields = Field(default_factory=StatedFields)
    markers: list[Chapter] = Field(default_factory=list)
    frames: list[str] = Field(default_factory=list)
    ocr: list[EvidenceOcrRef] = Field(default_factory=list)
    clip: str | None = None
    alignment_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    alignment: Literal["low"] | None = None
    gaps: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    schema_version: str = SCHEMA_VERSION
    provider: str
    model: str
    prompt_version: str
    session_id: str
    trades: list[EvidenceTrade] = Field(default_factory=list)


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


RuleStatus = Literal["pass", "violated", "unverifiable"]


class RuleCheck(BaseModel):
    rule: str
    status: RuleStatus
    evidence: dict = Field(default_factory=dict)
    reason: str | None = None


class RulesReport(BaseModel):
    schema_version: str = SCHEMA_VERSION
    session_id: str
    rules: list[RuleCheck] = Field(default_factory=list)


class BriefContext(BaseModel):
    macro_url: str | None = None
    ny_url: str | None = None
    bias_nq: str | None = None
    bias_es: str | None = None
    conviction: str | None = None
    kill_levels: str | None = None
    quoted: Literal[True] = True


class DrcContext(BaseModel):
    scores: dict[str, str] = Field(default_factory=dict)
    url: str


class LabTradeContext(BaseModel):
    nearest_level_token: str | None = None
    level_context: str | None = None
    tag_alignment: str | None = None
    inferred_triggers_1m: str | None = None
    zone_id: str | None = None


class LabContext(BaseModel):
    per_trade: dict[str, LabTradeContext] = Field(default_factory=dict)


class SessionContext(BaseModel):
    schema_version: str = SCHEMA_VERSION
    session_id: str
    brief: BriefContext | None = None
    drc: DrcContext | None = None
    lab: LabContext | None = None
    gaps: list[str] = Field(default_factory=list)


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
