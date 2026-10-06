"""Fill-span trading hours for the day path (PR-35, plan §2.6 / CD3(c))."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from tradevidanalyser.day_fills import DayOwnership, clip_has_core
from tradevidanalyser.day_manifest import DayManifestError
from tradevidanalyser.schema import SessionRecord

REASON_PAUSED = "paused_clip"
REASON_SPAN_UNDEFINED = "span_undefined"
REASON_SPAN_ZERO_EXCLUDED = "span_zero_excluded"
REASON_OUTSIDE = "outside_trades_present"
REASON_MIXED = "mixed_basis"
BASIS_DURATION = "duration"
BASIS_FILL_SPAN = "fill_span"
BASIS_MIXED = "mixed"


@dataclass(frozen=True)
class DayHours:
    trade_count: int
    hours: float
    trades_outside_clips: int
    trades_span_zero: int
    reason: str | None
    entitled_cores: int


def compute_day_hours(
    root: Path,
    day: date,
    ownership: DayOwnership,
    clips: list[SessionRecord],
) -> DayHours:
    del day
    entitled = [record for record in clips if clip_has_core(record, root)]
    span_s_by_session: dict[str, float] = {}
    intervals: list[tuple[datetime, datetime]] = []
    for record in entitled:
        fills = list(ownership.assigned.get(record.id) or [])
        if len(fills) < 2:
            span_s_by_session[record.id] = 0.0
            continue
        times = sorted(_utc(fill.timestamp) for fill in fills)
        start, end = times[0], times[-1]
        span_s_by_session[record.id] = (end - start).total_seconds()
        intervals.append((start, end))
    hours = _union_hours(intervals)
    outside = 0
    span_zero = 0
    counted = 0
    for trade in ownership.trades:
        owner = ownership.fill_owner.get(trade.entry_fill_id)
        if owner is None:
            outside += 1
            continue
        if span_s_by_session.get(owner, 0.0) <= 0:
            span_zero += 1
            continue
        counted += 1
    return DayHours(
        trade_count=counted,
        hours=hours,
        trades_outside_clips=outside,
        trades_span_zero=span_zero,
        reason=day_hours_reason(
            entitled_cores=len(entitled),
            hours=hours,
            trades_span_zero=span_zero,
            trades_outside_clips=outside,
        ),
        entitled_cores=len(entitled),
    )


def day_hours_reason(
    *,
    entitled_cores: int,
    hours: float,
    trades_span_zero: int,
    trades_outside_clips: int,
    hours_basis: str | None = None,
) -> str | None:
    if entitled_cores <= 0:
        return REASON_PAUSED
    if hours <= 0:
        return REASON_SPAN_UNDEFINED
    if trades_span_zero > 0:
        return REASON_SPAN_ZERO_EXCLUDED
    if trades_outside_clips > 0:
        return REASON_OUTSIDE
    if hours_basis == BASIS_MIXED:
        return REASON_MIXED
    return None


def period_hours_reason(
    *,
    entitled_cores: int,
    hours: float,
    trades_span_zero: int,
    trades_outside_clips: int,
    hours_basis: str | None,
) -> str | None:
    return day_hours_reason(
        entitled_cores=entitled_cores,
        hours=hours,
        trades_span_zero=trades_span_zero,
        trades_outside_clips=trades_outside_clips,
        hours_basis=hours_basis,
    )


def _union_hours(intervals: list[tuple[datetime, datetime]]) -> float:
    if not intervals:
        return 0.0
    ordered = sorted(intervals, key=lambda item: (item[0], item[1]))
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return sum((end - start).total_seconds() for start, end in merged) / 3600.0


def _utc(value: datetime) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise DayManifestError(f"timestamp is not datetime: {value!r}")
    if value.tzinfo is None:
        raise DayManifestError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)
