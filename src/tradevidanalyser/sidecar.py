"""OBS sidecar reader and ``time_anchor.json`` (PR-40). Opt-in via ``TVA_SIDECAR``.

Q-15 = B: measure the first-frame offset per clip as
``wall_span − video_span`` (TT:439–440): record_stop−record_start wall,
minus pause wall spans, minus ffprobe duration. ffprobe ``start_time``
below 0.1 s is unusable (container default), never a measured value.
1.043 s is a loud fallback (warning + ``offset_source: fallback``), never silent.

First-frame validity is two measured bands (plan amendment 2026-10-30):
low 0.20–0.45 s and high 0.80–1.30 s. ``time_anchor.json`` stamps
``offset_mode: low|high``. The gap and anything outside both bands fail
loudly. ``record_start`` without ``record_stop`` is ``incomplete``.
"""

from __future__ import annotations

import json
import logging
import math
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from tradevidanalyser import media, store
from tradevidanalyser.flags import sidecar_enabled
from tradevidanalyser.media import MediaError
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.schema import SCHEMA_VERSION, SessionRecord
from tradevidanalyser.sidecar_copy import sidecar_source_path

FIRST_FRAME_OFFSET_EXPECTED_S = 1.043
FIRST_FRAME_OFFSET_LOW_MIN_S = 0.20
FIRST_FRAME_OFFSET_LOW_MAX_S = 0.45
FIRST_FRAME_OFFSET_HIGH_MIN_S = 0.80
FIRST_FRAME_OFFSET_HIGH_MAX_S = 1.30
# Historical single-band names: the high band (kept for callers / docs).
FIRST_FRAME_OFFSET_MIN_S = FIRST_FRAME_OFFSET_HIGH_MIN_S
FIRST_FRAME_OFFSET_MAX_S = FIRST_FRAME_OFFSET_HIGH_MAX_S
RESUME_OFFSET_MIN_S = 0.4
RESUME_OFFSET_MAX_S = 0.8
SPAN_RESIDUAL_MAX_S = 0.10
DURATION_TOL_S = 0.10

STATUS_VERIFIED = "verified"
STATUS_PARTIAL = "partial"
STATUS_INVALID = "invalid"
STATUS_INCOMPLETE = "incomplete"
STATUS_NO_SIDECAR = "no_sidecar"
STATUS_SIDECAR_MISMATCH = "sidecar_mismatch"

REASON_FIRST_FRAME = "first_frame_offset_out_of_band"
REASON_RESUME = "resume_offset_out_of_band"
REASON_SEGMENT_INVALID = "segment_invalid"
REASON_SPAN = "span_residual_out_of_band"
REASON_OUTSIDE = "outside_sections"
REASON_INVALID_JSON = "invalid_json"
REASON_INCOMPLETE = "missing_record_stop"

START_TIME_USABLE_MIN_S = 0.1

_LOG = logging.getLogger(__name__)

SOURCE_MEASURED = "measured"
SOURCE_FIXED = "fixed"
OFFSET_SOURCE_FALLBACK = "fallback"
OFFSET_MODE_LOW = "low"
OFFSET_MODE_HIGH = "high"

EVENT_RECORD_START = "record_start"
EVENT_RECORD_STOP = "record_stop"
EVENT_PAUSE = "pause"
EVENT_RESUME = "resume"
EVENT_FILE_CHANGED = "file_changed"

METHOD_SIDECAR = "sidecar"

FALLBACK_WARNING = (
    "Erstbild-Versatz: Messung fehlgeschlagen, Fallback 1.043 s "
    "(offset_source=fallback)"
)
INCOMPLETE_WARNING = (
    "Sidecar unvollständig: record_start ohne record_stop "
    "(status=incomplete, kein first-frame offset)"
)

AnchorStatus = Literal[
    "verified",
    "partial",
    "invalid",
    "incomplete",
    "no_sidecar",
    "sidecar_mismatch",
]
SectionStatus = Literal["verified", "segment_invalid"]
OffsetSource = Literal["measured", "fixed"]
OffsetMode = Literal["low", "high"]


class SidecarFallbackWarning(UserWarning):
    """Loud first-frame fallback; never a silent substitute (TT:441, Q-15 = B)."""


class SidecarIncompleteWarning(UserWarning):
    """Loud incomplete sidecar: record_start without record_stop (TT:444)."""


@dataclass(frozen=True)
class SidecarEvent:
    event: str
    wall: datetime | None = None
    video_s: float | None = None
    file: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SidecarFill:
    fill_id: str
    wall: datetime


@dataclass
class TimeAnchorSection:
    index: int
    status: SectionStatus
    start_event: str
    end_event: str
    wall_start: str
    wall_end: str
    video_s_start: float
    video_s_end: float
    reason: str | None = None
    method: str | None = None
    resume_offset_s: float | None = None
    file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "index": self.index,
            "status": self.status,
            "method": self.method,
            "start_event": self.start_event,
            "end_event": self.end_event,
            "wall_start": self.wall_start,
            "wall_end": self.wall_end,
            "video_s_start": self.video_s_start,
            "video_s_end": self.video_s_end,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.resume_offset_s is not None:
            payload["resume_offset_s"] = self.resume_offset_s
        if self.file:
            payload["file"] = self.file
        return payload


@dataclass
class TimeAnchorFill:
    fill_id: str
    video_s: float | None
    reason: str | None = None
    section: int | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "fill_id": self.fill_id,
            "video_s": self.video_s,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.section is not None:
            payload["section"] = self.section
        return payload


@dataclass
class TimeAnchor:
    session_id: str
    status: AnchorStatus
    schema_version: str = SCHEMA_VERSION
    reason: str | None = None
    method: str | None = None
    first_frame_offset_s: float | None = None
    first_frame_offset_source: OffsetSource | None = None
    offset_source: str | None = None
    offset_mode: OffsetMode | None = None
    file: str | None = None
    video_duration_s: float | None = None
    sidecar_duration_s: float | None = None
    sections: list[TimeAnchorSection] = field(default_factory=list)
    fills: list[TimeAnchorFill] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "status": self.status,
        }
        if self.reason:
            payload["reason"] = self.reason
        payload["method"] = self.method
        if self.first_frame_offset_s is not None:
            payload["first_frame_offset_s"] = self.first_frame_offset_s
        if self.first_frame_offset_source:
            payload["first_frame_offset_source"] = self.first_frame_offset_source
        if self.offset_source:
            payload["offset_source"] = self.offset_source
        if self.offset_mode:
            payload["offset_mode"] = self.offset_mode
        if self.file:
            payload["file"] = self.file
        if self.video_duration_s is not None:
            payload["video_duration_s"] = self.video_duration_s
        if self.sidecar_duration_s is not None:
            payload["sidecar_duration_s"] = self.sidecar_duration_s
        payload["sections"] = [item.as_dict() for item in self.sections]
        if self.fills:
            payload["fills"] = [item.as_dict() for item in self.fills]
        return payload


def time_anchor_path(root: Path, session_id: str) -> Path:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return store.time_anchor_path(root, session_id)


def sidecar_replaces_detector(anchor: TimeAnchor | None) -> bool:
    """Verified/partial sidecar pauses replace the OCR detector (TT:902)."""
    return anchor is not None and anchor.status in {STATUS_VERIFIED, STATUS_PARTIAL}


def parse_sidecar_jsonl(path: Path) -> list[SidecarEvent]:
    """Parse ``<stem>.jsonl``. Empty / missing file → empty list (caller decides)."""
    text = path.read_text(encoding="utf-8")
    events: list[SidecarEvent] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        raw = json.loads(stripped)
        if not isinstance(raw, dict):
            raise ValueError("sidecar line is not a JSON object")
        events.append(_event_from_raw(raw))
    return events


def probe_start_time_s(probe: dict[str, Any] | None) -> float | None:
    """ffprobe first-packet start_time. Values below 0.1 s are unusable."""
    if not probe:
        return None
    found: float | None = None
    for stream in probe.get("streams") or []:
        if stream.get("codec_type") != "video":
            continue
        value = _finite_float(stream.get("start_time"))
        if value is not None:
            found = value
            break
    if found is None:
        fmt = probe.get("format") or {}
        found = _finite_float(fmt.get("start_time"))
    if found is None or found < START_TIME_USABLE_MIN_S:
        return None
    return found


def _emit_fallback_warning() -> None:
    """Fire on every clip. Default ``once`` filtering would hide later clips."""
    _LOG.warning(FALLBACK_WARNING)
    with warnings.catch_warnings():
        warnings.simplefilter("always", SidecarFallbackWarning)
        warnings.warn(FALLBACK_WARNING, SidecarFallbackWarning, stacklevel=3)


def _emit_incomplete_warning() -> None:
    """Fire on every clip. Default ``once`` filtering would hide later clips."""
    _LOG.warning(INCOMPLETE_WARNING)
    with warnings.catch_warnings():
        warnings.simplefilter("always", SidecarIncompleteWarning)
        warnings.warn(INCOMPLETE_WARNING, SidecarIncompleteWarning, stacklevel=3)


def classify_first_frame_offset(offset: float) -> OffsetMode | None:
    """Return ``low`` / ``high`` when *offset* is in a validity band, else None."""
    if FIRST_FRAME_OFFSET_LOW_MIN_S <= offset <= FIRST_FRAME_OFFSET_LOW_MAX_S:
        return OFFSET_MODE_LOW
    if FIRST_FRAME_OFFSET_HIGH_MIN_S <= offset <= FIRST_FRAME_OFFSET_HIGH_MAX_S:
        return OFFSET_MODE_HIGH
    return None


def _pause_wall_span_s(events: list[SidecarEvent]) -> float | None:
    """Sum of ``resume.wall − pause.wall``. None if a pause pair is incomplete."""
    total = 0.0
    last_pause: SidecarEvent | None = None
    for item in events:
        if item.event == EVENT_PAUSE:
            last_pause = item
            continue
        if item.event == EVENT_RESUME:
            if last_pause is None or last_pause.wall is None or item.wall is None:
                return None
            total += (item.wall - last_pause.wall).total_seconds()
            last_pause = None
            continue
        if item.event == EVENT_RECORD_STOP and last_pause is not None:
            return None
    if last_pause is not None:
        return None
    return total


def _wall_span_minus_video_s(
    events: list[SidecarEvent], video_duration_s: float | None
) -> float | None:
    """``wall_span − video_span`` (TT:439–440). Pause wall time is not media (TT:449)."""
    start = next((item for item in events if item.event == EVENT_RECORD_START), None)
    stop = next((item for item in reversed(events) if item.event == EVENT_RECORD_STOP), None)
    if start is None or stop is None or start.wall is None or stop.wall is None:
        return None
    if video_duration_s is None or not math.isfinite(video_duration_s):
        return None
    pause_span = _pause_wall_span_s(events)
    if pause_span is None:
        return None
    wall_span = (stop.wall - start.wall).total_seconds() - pause_span
    return wall_span - float(video_duration_s)


def measure_first_frame_offset(
    events: list[SidecarEvent],
    video_duration_s: float | None,
    *,
    probe: dict[str, Any] | None = None,
) -> tuple[float, OffsetSource, str | None]:
    """Q-15 = B: measure per clip as ``wall_span − video_span`` (TT:439–440).

    ``ffprobe`` ``start_time`` is not the offset (TT:441 only names it as an
    example). Values below 0.1 s are ignored. Failed measurement → 1.043 s
    + fallback flag, never silent (TT:441).
    """
    probe_start_time_s(probe)  # classify only; never treat < 0.1 as measured
    measured = _wall_span_minus_video_s(events, video_duration_s)
    if measured is None:
        _emit_fallback_warning()
        return FIRST_FRAME_OFFSET_EXPECTED_S, SOURCE_FIXED, OFFSET_SOURCE_FALLBACK
    return _r(measured), SOURCE_MEASURED, None


def build_time_anchor(
    *,
    session_id: str,
    video_name: str,
    sidecar_path: Path | None = None,
    events: list[SidecarEvent] | None = None,
    probe: dict[str, Any] | None = None,
    duration_s: float | None = None,
    fills: list[SidecarFill] | None = None,
    measured_offset_s: float | None = None,
) -> TimeAnchor:
    """Build a time anchor from sidecar events + optional ffprobe facts.

    ``measured_offset_s`` injects a successful measurement (tests). Otherwise
    the offset is measured from *probe* (Q-15 = B).
    """
    if events is None:
        if sidecar_path is None or not sidecar_path.is_file():
            return TimeAnchor(session_id=session_id, status=STATUS_NO_SIDECAR)
        try:
            events = parse_sidecar_jsonl(sidecar_path)
        except FileNotFoundError:
            return TimeAnchor(session_id=session_id, status=STATUS_NO_SIDECAR)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return TimeAnchor(
                session_id=session_id,
                status=STATUS_INVALID,
                reason=REASON_INVALID_JSON,
            )

    ours = _events_for_file(events, video_name)
    has_start = any(item.event == EVENT_RECORD_START for item in ours)
    if not has_start:
        if any(item.event == EVENT_RECORD_START for item in events):
            return TimeAnchor(
                session_id=session_id,
                status=STATUS_SIDECAR_MISMATCH,
                file=video_name,
            )
        return TimeAnchor(session_id=session_id, status=STATUS_NO_SIDECAR)

    start_event = next(item for item in ours if item.event == EVENT_RECORD_START)
    if not _file_matches(start_event.file, video_name):
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_SIDECAR_MISMATCH,
            file=video_name,
        )

    video_duration = duration_s
    if video_duration is None and probe is not None:
        video_duration = media.duration_s(probe)

    if not any(item.event == EVENT_RECORD_STOP for item in ours):
        _emit_incomplete_warning()
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_INCOMPLETE,
            reason=REASON_INCOMPLETE,
            file=video_name,
            video_duration_s=_opt_round(video_duration),
        )

    sidecar_duration = _sidecar_duration_s(ours)
    if video_duration is None or sidecar_duration is None:
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_SIDECAR_MISMATCH,
            file=video_name,
            video_duration_s=_opt_round(video_duration),
            sidecar_duration_s=_opt_round(sidecar_duration),
        )
    if abs(sidecar_duration - video_duration) > DURATION_TOL_S:
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_SIDECAR_MISMATCH,
            file=video_name,
            video_duration_s=_r(video_duration),
            sidecar_duration_s=_r(sidecar_duration),
        )

    if measured_offset_s is not None:
        offset = _r(measured_offset_s)
        source: OffsetSource = SOURCE_MEASURED
        offset_source = None
    else:
        offset, source, offset_source = measure_first_frame_offset(
            ours, video_duration, probe=probe
        )

    offset_mode = classify_first_frame_offset(offset)
    if offset_mode is None:
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_INVALID,
            reason=REASON_FIRST_FRAME,
            file=video_name,
            first_frame_offset_s=offset,
            first_frame_offset_source=source,
            offset_source=offset_source,
            video_duration_s=_r(video_duration),
            sidecar_duration_s=_r(sidecar_duration),
        )

    sections, partial = _build_sections(ours, video_name)
    residual = _span_residual(ours, video_duration, offset)
    if residual is not None and residual > SPAN_RESIDUAL_MAX_S:
        return TimeAnchor(
            session_id=session_id,
            status=STATUS_INVALID,
            reason=REASON_SPAN,
            file=video_name,
            first_frame_offset_s=offset,
            first_frame_offset_source=source,
            offset_source=offset_source,
            offset_mode=offset_mode,
            video_duration_s=_r(video_duration),
            sidecar_duration_s=_r(sidecar_duration),
        )

    status: AnchorStatus = STATUS_PARTIAL if partial else STATUS_VERIFIED
    method = METHOD_SIDECAR
    fill_rows = _map_fills(fills or [], sections, ours, offset) if fills else []
    return TimeAnchor(
        session_id=session_id,
        status=status,
        method=method,
        file=video_name,
        first_frame_offset_s=offset,
        first_frame_offset_source=source,
        offset_source=offset_source,
        offset_mode=offset_mode,
        video_duration_s=_r(video_duration),
        sidecar_duration_s=_r(sidecar_duration),
        sections=sections,
        fills=fill_rows,
    )


def write_time_anchor(root: Path, anchor: TimeAnchor) -> Path:
    path = time_anchor_path(root, anchor.session_id)
    store.write_json(path, anchor.as_dict())
    return path


def load_time_anchor(root: Path, session_id: str) -> TimeAnchor | None:
    path = time_anchor_path(root, session_id)
    if not path.is_file():
        return None
    try:
        raw = store.read_json(path)
    except (OSError, ValueError):
        return None
    return _anchor_from_payload(raw)


def write_session_time_anchor(
    record: SessionRecord,
    *,
    root: Path,
    probe: dict[str, Any] | None = None,
    duration_s: float | None = None,
    fills: list[SidecarFill] | None = None,
    measured_offset_s: float | None = None,
) -> TimeAnchor | None:
    """Write ``sessions/<id>/time_anchor.json`` when ``TVA_SIDECAR`` is on."""
    if not sidecar_enabled():
        return None
    session_id = record.id
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    video = _video_path(record, root)
    sidecar = sidecar_source_path(video) if video is not None else None
    video_name = record.recording.filename or (video.name if video is not None else "")
    resolved_probe = probe
    resolved_duration = duration_s
    if resolved_probe is None and video is not None and video.is_file():
        try:
            resolved_probe = media.ffprobe(video)
        except (MediaError, OSError, ValueError):
            resolved_probe = None
    if resolved_duration is None and resolved_probe is not None:
        resolved_duration = media.duration_s(resolved_probe)
    if resolved_duration is None:
        resolved_duration = float(record.recording.duration_s)
    if fills is None:
        fills = _fills_from_store(root, session_id)
    anchor = build_time_anchor(
        session_id=session_id,
        video_name=video_name,
        sidecar_path=sidecar,
        probe=resolved_probe,
        duration_s=resolved_duration,
        fills=fills,
        measured_offset_s=measured_offset_s,
    )
    write_time_anchor(root, anchor)
    return anchor


def fill_video_s(
    wall: datetime,
    anchor: TimeAnchor,
    *,
    record_start: datetime | None = None,
) -> tuple[float | None, str | None]:
    """Map a fill wall clock onto sidecar media time. Invalid section → ``None``."""
    if not anchor.sections:
        return None, REASON_OUTSIDE
    start = record_start or _parse_wall(anchor.sections[0].wall_start)
    if start is None:
        return None, REASON_OUTSIDE
    offset = anchor.first_frame_offset_s or 0.0
    mapped = _map_fills(
        [SidecarFill(fill_id="_", wall=wall)],
        anchor.sections,
        [
            SidecarEvent(
                event=EVENT_RECORD_START,
                wall=start,
                video_s=0.0,
            )
        ],
        offset,
    )
    if not mapped:
        return None, REASON_OUTSIDE
    return mapped[0].video_s, mapped[0].reason


def _event_from_raw(raw: dict[str, Any]) -> SidecarEvent:
    name = str(raw.get("event") or "").strip()
    file_name = raw.get("file")
    file_value = Path(str(file_name)).name if file_name else None
    return SidecarEvent(
        event=name,
        wall=_parse_wall(raw.get("wall") if raw.get("wall") is not None else raw.get("ts")),
        video_s=_finite_float(raw.get("video_s")),
        file=file_value,
        raw=raw,
    )


def _events_for_file(events: list[SidecarEvent], video_name: str) -> list[SidecarEvent]:
    current: str | None = None
    for item in events:
        if item.event == EVENT_RECORD_START and item.file:
            current = item.file
            break
        if item.event == EVENT_FILE_CHANGED and item.file:
            current = item.file
            break
    kept: list[SidecarEvent] = []
    for item in events:
        if item.event == EVENT_FILE_CHANGED and item.file:
            current = item.file
            continue
        if item.file:
            current = item.file
        if current is None or _file_matches(current, video_name):
            kept.append(item)
    return kept


def _file_matches(sidecar_file: str | None, video_name: str) -> bool:
    if not sidecar_file or not video_name:
        return False
    return Path(sidecar_file).name == Path(video_name).name


def _sidecar_duration_s(events: list[SidecarEvent]) -> float | None:
    start = next((item.video_s for item in events if item.event == EVENT_RECORD_START), None)
    stop = next((item.video_s for item in reversed(events) if item.event == EVENT_RECORD_STOP), None)
    if start is not None and stop is not None:
        return stop - start
    values = [item.video_s for item in events if item.video_s is not None]
    if len(values) >= 2:
        return values[-1] - values[0]
    if len(values) == 1:
        return values[0]
    return None


def _span_residual(
    events: list[SidecarEvent], video_duration_s: float | None, offset: float
) -> float | None:
    """``|wall_span − video_span − first_frame_offset|`` (TT:464)."""
    span = _wall_span_minus_video_s(events, video_duration_s)
    if span is None:
        return None
    return abs(span - offset)


def section_span_residual(
    section: TimeAnchorSection,
    *,
    first_frame_offset_s: float = FIRST_FRAME_OFFSET_EXPECTED_S,
) -> float | None:
    """Per-section span residual from N5 wall/video bounds (TT:464 / TT:902).

    The first-frame offset applies only on the ``record_start`` section. Later
    sections already sit on media time (R13: resume continues at pause
    ``video_s``), so their residual is ``|wall_span − video_span|``.
    """
    wall_start = _parse_wall(section.wall_start)
    wall_end = _parse_wall(section.wall_end)
    if wall_start is None or wall_end is None:
        return None
    wall_span = (wall_end - wall_start).total_seconds()
    video_span = float(section.video_s_end) - float(section.video_s_start)
    offset = first_frame_offset_s if section.start_event == EVENT_RECORD_START else 0.0
    return _r(abs(wall_span - video_span - offset))


def _build_sections(
    events: list[SidecarEvent], video_name: str
) -> tuple[list[TimeAnchorSection], bool]:
    start = next((item for item in events if item.event == EVENT_RECORD_START), None)
    if start is None or start.wall is None or start.video_s is None:
        return [], False

    sections: list[TimeAnchorSection] = []
    open_event = start
    open_video_s = start.video_s
    last_pause: SidecarEvent | None = None
    invalidate_from: int | None = None
    pending_resume_offset: float | None = None

    def close(end: SidecarEvent, *, resume_offset: float | None) -> None:
        if end.wall is None or end.video_s is None or open_event.wall is None:
            return
        index = len(sections) + 1
        invalid = invalidate_from is not None and index >= invalidate_from
        sections.append(
            TimeAnchorSection(
                index=index,
                status="segment_invalid" if invalid else "verified",
                reason=REASON_RESUME if invalid else None,
                method=None if invalid else METHOD_SIDECAR,
                start_event=open_event.event,
                end_event=end.event,
                wall_start=open_event.wall.isoformat(),
                wall_end=end.wall.isoformat(),
                video_s_start=_r(open_video_s),
                video_s_end=_r(end.video_s),
                resume_offset_s=_opt_round(resume_offset),
                file=video_name,
            )
        )

    for item in events:
        if item is start:
            continue
        if item.event == EVENT_PAUSE:
            close(item, resume_offset=pending_resume_offset)
            pending_resume_offset = None
            last_pause = item
            continue
        if item.event == EVENT_RESUME:
            if last_pause is None or last_pause.video_s is None or item.video_s is None:
                continue
            resume_offset = item.video_s - last_pause.video_s
            if not (RESUME_OFFSET_MIN_S <= resume_offset <= RESUME_OFFSET_MAX_S):
                if invalidate_from is None:
                    invalidate_from = len(sections) + 1
            open_event = item
            open_video_s = last_pause.video_s
            pending_resume_offset = _r(resume_offset)
            last_pause = None
            continue
        if item.event == EVENT_RECORD_STOP:
            close(item, resume_offset=pending_resume_offset)
            pending_resume_offset = None

    partial = any(item.status == "segment_invalid" for item in sections)
    return sections, partial


def _map_fills(
    fills: list[SidecarFill],
    sections: list[TimeAnchorSection],
    events: list[SidecarEvent],
    offset: float,
) -> list[TimeAnchorFill]:
    start = next((item for item in events if item.event == EVENT_RECORD_START), None)
    record_start = start.wall if start is not None else None
    out: list[TimeAnchorFill] = []
    parsed: list[tuple[TimeAnchorSection, datetime, datetime]] = []
    for section in sections:
        wall_start = _parse_wall(section.wall_start)
        wall_end = _parse_wall(section.wall_end)
        if wall_start is None or wall_end is None:
            continue
        parsed.append((section, wall_start, wall_end))
    for fill in fills:
        wall = _as_aware(fill.wall)
        match: TimeAnchorSection | None = None
        match_start: datetime | None = None
        for index, (section, wall_start, wall_end) in enumerate(parsed):
            last = index == len(parsed) - 1
            if wall_start <= wall < wall_end or (last and wall == wall_end):
                match = section
                match_start = wall_start
                break
        if match is None or match_start is None:
            out.append(TimeAnchorFill(fill_id=fill.fill_id, video_s=None, reason=REASON_OUTSIDE))
            continue
        if match.status == "segment_invalid":
            out.append(
                TimeAnchorFill(
                    fill_id=fill.fill_id,
                    video_s=None,
                    reason=REASON_SEGMENT_INVALID,
                    section=match.index,
                )
            )
            continue
        if match.start_event == EVENT_RECORD_START and record_start is not None:
            video_s = (wall - record_start).total_seconds() - offset
        else:
            video_s = match.video_s_start + (wall - match_start).total_seconds()
        out.append(
            TimeAnchorFill(
                fill_id=fill.fill_id,
                video_s=_r(video_s),
                section=match.index,
            )
        )
    return out


def _video_path(record: SessionRecord, root: Path) -> Path | None:
    raw = record.recording.path
    if not raw:
        name = record.recording.filename
        if not name:
            return None
        return store.require_under_root(root / "recordings" / name, root)
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        return store.require_under_root(path, root)
    except ValueError:
        return None


def _fills_from_store(root: Path, session_id: str) -> list[SidecarFill]:
    path = store.fills_path(root, session_id)
    if not path.is_file():
        return []
    try:
        import pyarrow.parquet as pq
    except ImportError:
        return []
    try:
        table = pq.read_table(path)
    except (OSError, ValueError):
        return []
    names = set(table.column_names)
    if "fill_id" not in names or "timestamp" not in names:
        return []
    fills: list[SidecarFill] = []
    for fill_id, stamp in zip(
        table.column("fill_id").to_pylist(),
        table.column("timestamp").to_pylist(),
        strict=True,
    ):
        if fill_id is None or stamp is None:
            continue
        if hasattr(stamp, "to_pydatetime"):
            stamp = stamp.to_pydatetime()
        if not isinstance(stamp, datetime):
            continue
        fills.append(SidecarFill(fill_id=str(fill_id), wall=_as_aware(stamp)))
    return fills


def _anchor_from_payload(raw: dict[str, Any]) -> TimeAnchor | None:
    status = raw.get("status")
    if status not in {
        STATUS_VERIFIED,
        STATUS_PARTIAL,
        STATUS_INVALID,
        STATUS_INCOMPLETE,
        STATUS_NO_SIDECAR,
        STATUS_SIDECAR_MISMATCH,
    }:
        return None
    version = raw.get("schema_version")
    if version is not None and str(version) != SCHEMA_VERSION:
        return None
    sections: list[TimeAnchorSection] = []
    for item in raw.get("sections") or []:
        if not isinstance(item, dict):
            continue
        sec_status = item.get("status")
        if sec_status not in {"verified", "segment_invalid"}:
            continue
        sections.append(
            TimeAnchorSection(
                index=int(item.get("index") or 0),
                status=sec_status,
                start_event=str(item.get("start_event") or ""),
                end_event=str(item.get("end_event") or ""),
                wall_start=str(item.get("wall_start") or ""),
                wall_end=str(item.get("wall_end") or ""),
                video_s_start=float(item.get("video_s_start") or 0.0),
                video_s_end=float(item.get("video_s_end") or 0.0),
                reason=str(item["reason"]) if item.get("reason") else None,
                method=str(item["method"]) if item.get("method") else None,
                resume_offset_s=_finite_float(item.get("resume_offset_s")),
                file=str(item["file"]) if item.get("file") else None,
            )
        )
    fills: list[TimeAnchorFill] = []
    for item in raw.get("fills") or []:
        if not isinstance(item, dict) or not item.get("fill_id"):
            continue
        fills.append(
            TimeAnchorFill(
                fill_id=str(item["fill_id"]),
                video_s=_finite_float(item.get("video_s")),
                reason=str(item["reason"]) if item.get("reason") else None,
                section=int(item["section"]) if item.get("section") is not None else None,
            )
        )
    source = raw.get("first_frame_offset_source")
    if source not in {None, SOURCE_MEASURED, SOURCE_FIXED}:
        source = None
    offset_mode = raw.get("offset_mode")
    if offset_mode not in {None, OFFSET_MODE_LOW, OFFSET_MODE_HIGH}:
        offset_mode = None
    return TimeAnchor(
        session_id=str(raw.get("session_id") or ""),
        status=status,
        schema_version=SCHEMA_VERSION,
        reason=str(raw["reason"]) if raw.get("reason") else None,
        method=str(raw["method"]) if raw.get("method") else None,
        first_frame_offset_s=_finite_float(raw.get("first_frame_offset_s")),
        first_frame_offset_source=source,
        offset_source=str(raw["offset_source"]) if raw.get("offset_source") else None,
        offset_mode=offset_mode,
        file=str(raw["file"]) if raw.get("file") else None,
        video_duration_s=_finite_float(raw.get("video_duration_s")),
        sidecar_duration_s=_finite_float(raw.get("sidecar_duration_s")),
        sections=sections,
        fills=fills,
    )


def _parse_wall(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _as_aware(value)
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return datetime.fromtimestamp(float(value), tz=VIENNA)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return _as_aware(parsed)


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=VIENNA)


def _finite_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _opt_round(value: float | None) -> float | None:
    if value is None:
        return None
    return _r(value)


def _r(value: float, ndigits: int = 6) -> float:
    out = round(float(value), ndigits)
    return 0.0 if out == 0.0 else out
