"""Per-trade evidence windows. Transcript + alignment + optional frames/ocr/clips."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.align import FILENAME_CONFIDENCE
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.ocr import read_ocr_parquet
from tradevidanalyser.providers.extract import (
    NO_SPEECH_GAP,
    STATED_PROMPT_FILENAME,
    STATED_PROMPT_VERSION_FAKE,
    default_prompt_path,
    apply_stated_citation_guard,
    evidence_citation_problems,
    get_extract_provider,
    prompt_version_for,
    stated_fields_of,
)
from tradevidanalyser.schema import (
    Alignment,
    Chapter,
    Evidence,
    EvidenceOcrRef,
    EvidenceTrade,
    EvidenceWindow,
    SessionRecord,
    Transcript,
    TranscriptSegment,
)

SCHEMA_VERSION = "1"
DEFAULT_PRE_S = 180.0
DEFAULT_POST_S = 120.0
ALIGNMENT_LOW = 0.8
ENV_PRE_S = "TVA_EVIDENCE_PRE_S"
ENV_POST_S = "TVA_EVIDENCE_POST_S"
_CLIP_T = re.compile(r"^(\d+\.\d{3})")


@dataclass(frozen=True)
class EvidenceResult:
    session_id: str
    status: str
    path: str | None = None
    trades: int = 0
    provider: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict:
        payload: dict = {
            "status": self.status,
            "session_id": self.session_id,
            "trades": self.trades,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _r(value: float, ndigits: int) -> float:
    out = round(float(value), ndigits)
    return 0.0 if out == 0.0 else out


def _require_safe_session_id(session_id: str) -> str:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return session_id


def _parse_dt(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _as_aware(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        return dt
    return dt.replace(tzinfo=VIENNA)


def _float_env(name: str, default: float) -> float:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def window_pads(*, pre_s: float | None = None, post_s: float | None = None) -> tuple[float, float]:
    if pre_s is None:
        pre = _float_env(ENV_PRE_S, DEFAULT_PRE_S)
    else:
        pre = float(pre_s)
        if not math.isfinite(pre) or pre < 0:
            pre = DEFAULT_PRE_S
    if post_s is None:
        post = _float_env(ENV_POST_S, DEFAULT_POST_S)
    else:
        post = float(post_s)
        if not math.isfinite(post) or post < 0:
            post = DEFAULT_POST_S
    return pre, post


def wall_to_video_t(
    wall: datetime,
    start: datetime,
    *,
    offset_s: float = 0.0,
    drift_s_per_h: float = 0.0,
) -> float:
    """Invert §3.5: wall = start + video_t + offset + drift * video_t / 3600."""
    wall_a = _as_aware(wall)
    start_a = _as_aware(start)
    delta = (wall_a - start_a).total_seconds()
    denom = 1.0 + float(drift_s_per_h) / 3600.0
    if abs(denom) < 1e-12:
        denom = 1.0
    return (delta - float(offset_s)) / denom


def effective_alignment(record: SessionRecord) -> Alignment:
    if record.alignment is not None:
        return record.alignment
    return Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=FILENAME_CONFIDENCE,
        method="filename",
        samples=[],
    )


def segments_in_window(
    segments: list[TranscriptSegment], t0: float, t1: float
) -> list[TranscriptSegment]:
    return [seg for seg in segments if seg.t0 <= t1 and seg.t1 >= t0]


def _clip_time(name: str) -> float | None:
    match = _CLIP_T.match(name)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _nearest_name(names: list[tuple[float, str]], target: float) -> str | None:
    if not names:
        return None
    return min(names, key=lambda item: (abs(item[0] - target), item[0], item[1]))[1]


def _frames_for_window(root: Path, session_id: str, t0: float, t1: float, entry_t: float) -> list[str]:
    items: list[tuple[float, str]] = []
    for path in store.list_frame_jpgs(root, session_id):
        try:
            t = float(path.stem)
        except ValueError:
            continue
        items.append((t, path.name))
    inside = [name for t, name in items if t0 <= t <= t1]
    if inside:
        return inside
    nearest = _nearest_name(items, entry_t)
    return [nearest] if nearest else []


def _ocr_for_window(root: Path, session_id: str, t0: float, t1: float, entry_t: float) -> list[EvidenceOcrRef]:
    path = store.ocr_path(root, session_id)
    if not path.is_file():
        return []
    rows = read_ocr_parquet(path)
    inside = [
        EvidenceOcrRef(t=_r(row.t, 3), roi=row.roi, text=row.text, parsed=row.parsed)
        for row in rows
        if t0 <= row.t <= t1
    ]
    if inside:
        return inside
    if not rows:
        return []
    nearest = min(rows, key=lambda row: (abs(row.t - entry_t), row.t, row.roi))
    return [EvidenceOcrRef(t=_r(nearest.t, 3), roi=nearest.roi, text=nearest.text, parsed=nearest.parsed)]


def _clip_for_window(root: Path, session_id: str, t0: float, t1: float, entry_t: float) -> str | None:
    items: list[tuple[float, str]] = []
    for path in store.list_clips(root, session_id):
        t = _clip_time(path.name)
        if t is None:
            continue
        items.append((t, path.name))
    inside = [(t, name) for t, name in items if t0 <= t <= t1]
    if inside:
        return _nearest_name(inside, entry_t)
    return _nearest_name(items, entry_t)


def _markers_in_window(chapters: list[Chapter], t0: float, t1: float) -> list[Chapter]:
    return [Chapter(t=ch.t, name=ch.name) for ch in chapters if t0 <= ch.t <= t1]


def _as_utc_dt(value: object) -> datetime | None:
    """Parse a trades.parquet timestamp. Naive values are UTC (fills convention)."""
    if value is None:
        return None
    if hasattr(value, "to_pydatetime") and not isinstance(value, datetime):
        try:
            value = value.to_pydatetime()
        except (TypeError, ValueError):
            return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = _parse_dt(value)
        if parsed is None:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def read_trade_rows(path: Path) -> list[tuple[str, datetime, datetime | None]]:
    table = pq.read_table(path)
    names = set(table.column_names)
    if "tva_trade_id" not in names or "entry_timestamp" not in names:
        return []
    ids = table.column("tva_trade_id").to_pylist()
    entries = table.column("entry_timestamp").to_pylist()
    exits = table.column("exit_timestamp").to_pylist() if "exit_timestamp" in names else [None] * len(ids)
    rows: list[tuple[str, datetime, datetime | None]] = []
    for tva_id, entry, exit_ts in zip(ids, entries, exits, strict=True):
        if not tva_id:
            continue
        entry_dt = _as_utc_dt(entry)
        if entry_dt is None:
            continue
        rows.append((str(tva_id), entry_dt, _as_utc_dt(exit_ts)))
    rows.sort(key=lambda item: (item[0], item[1]))
    return rows


def _prompt_version(provider_name: str) -> str:
    if provider_name == "fake":
        return STATED_PROMPT_VERSION_FAKE
    try:
        return prompt_version_for(default_prompt_path(STATED_PROMPT_FILENAME))
    except Exception:
        return STATED_PROMPT_FILENAME


def _window_bounds(
    entry_t: float,
    exit_t: float,
    *,
    pre_s: float,
    post_s: float,
    duration_s: float,
) -> tuple[float, float]:
    if exit_t < entry_t:
        exit_t = entry_t
    t0 = entry_t - pre_s
    t1 = exit_t + post_s
    t0 = max(0.0, t0)
    if duration_s > 0 and math.isfinite(duration_s):
        t0 = min(t0, duration_s)
        t1 = min(duration_s, t1)
    if t1 < t0:
        t1 = t0
    return _r(t0, 3), _r(t1, 3)


def build_evidence(
    record: SessionRecord,
    *,
    root: Path,
    transcript: Transcript | None,
    provider_name: str | None = None,
    pre_s: float | None = None,
    post_s: float | None = None,
) -> Evidence | None:
    session_id = _require_safe_session_id(record.id)
    trades_file = store.trades_path(root, session_id)
    if not trades_file.is_file():
        return None
    rows = read_trade_rows(trades_file)
    if not rows:
        return None
    provider = get_extract_provider(provider_name)
    clock = effective_alignment(record)
    start = _parse_dt(record.recording.start_wallclock_vienna)
    if start is None:
        raise ValueError(f"unparseable start_wallclock_vienna {record.recording.start_wallclock_vienna!r}")
    start = _as_aware(start)
    pre, post = window_pads(pre_s=pre_s, post_s=post_s)
    duration = float(record.recording.duration_s or 0.0)
    segments = list(transcript.segments) if transcript is not None else []
    align_flag = "low" if clock.confidence < ALIGNMENT_LOW else None
    trades: list[EvidenceTrade] = []
    for tva_id, entry_ts, exit_ts in rows:
        entry_t = wall_to_video_t(
            entry_ts, start, offset_s=clock.offset_s, drift_s_per_h=clock.drift_s_per_h
        )
        exit_t = (
            wall_to_video_t(
                exit_ts, start, offset_s=clock.offset_s, drift_s_per_h=clock.drift_s_per_h
            )
            if exit_ts is not None
            else entry_t
        )
        t0, t1 = _window_bounds(entry_t, exit_t, pre_s=pre, post_s=post, duration_s=duration)
        window_segs = segments_in_window(segments, t0, t1)
        window_tx = Transcript(
            provider=transcript.provider if transcript else "none",
            model=transcript.model if transcript else "none",
            language=record.language,
            prompt_version=transcript.prompt_version if transcript else "",
            segments=window_segs,
        )
        stated_pass = apply_stated_citation_guard(window_tx, provider.stated_fields(window_tx))
        gaps = list(stated_pass.gaps)
        if not _has_window_speech(window_segs) and NO_SPEECH_GAP not in gaps:
            gaps.append(NO_SPEECH_GAP)
        trades.append(
            EvidenceTrade(
                tva_trade_id=tva_id,
                window=EvidenceWindow(t0=t0, t1=t1),
                commentary=[seg.id for seg in window_segs],
                stated=stated_fields_of(stated_pass),
                markers=_markers_in_window(record.recording.chapters, t0, t1),
                frames=_frames_for_window(root, session_id, t0, t1, entry_t),
                ocr=_ocr_for_window(root, session_id, t0, t1, entry_t),
                clip=_clip_for_window(root, session_id, t0, t1, entry_t),
                alignment_confidence=clock.confidence,
                alignment=align_flag,
                gaps=sorted(set(gaps)),
            )
        )
    return Evidence(
        schema_version=SCHEMA_VERSION,
        provider=provider.name,
        model=provider.model,
        prompt_version=_prompt_version(provider.name),
        session_id=session_id,
        trades=trades,
    )


def _has_window_speech(segments: list[TranscriptSegment]) -> bool:
    return any((seg.text or "").strip() for seg in segments)


def evidence_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
    pre_s: float | None = None,
    post_s: float | None = None,
) -> EvidenceResult:
    session_id = _require_safe_session_id(session_id)
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    transcript: Transcript | None = None
    if store.transcript_path(root, session_id).is_file():
        transcript = store.load_transcript(root, session_id)
    evidence = build_evidence(
        record,
        root=root,
        transcript=transcript,
        provider_name=provider_name,
        pre_s=pre_s,
        post_s=post_s,
    )
    if evidence is None:
        path = store.evidence_path(root, session_id)
        path.unlink(missing_ok=True)
        store.compute_status(root, session_id)
        return EvidenceResult(
            session_id=session_id,
            status="skipped",
            reason="no trades in session",
        )
    if transcript is not None:
        problems = evidence_citation_problems(transcript, evidence)
        if problems:
            raise ValueError(problems[0][2])
    store.write_json(store.evidence_path(root, session_id), evidence.model_dump(mode="json"))
    store.compute_status(root, session_id)
    return EvidenceResult(
        session_id=session_id,
        status="ok",
        path="evidence.json",
        trades=len(evidence.trades),
        provider=evidence.provider,
    )
