"""Session Record v1 — files on TVA_ROOT are the source of truth."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1"


class Chapter(BaseModel):
    t: float
    name: str = ""


class RecordingInfo(BaseModel):
    path: str
    sha256: str
    start_wallclock_vienna: str
    duration_s: float
    tracks: list[str] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    filename: str = ""


class SessionRecord(BaseModel):
    schema_version: str = SCHEMA_VERSION
    id: str
    recording: RecordingInfo
    language: str = "de"
    alignment: dict | None = None
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


StageState = Literal["ok", "missing", "failed", "ingesting"]


class SessionStatus(BaseModel):
    schema_version: str = SCHEMA_VERSION
    session_id: str
    stages: dict[str, StageState] = Field(default_factory=dict)
    error: str | None = None


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
