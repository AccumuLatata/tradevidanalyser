"""Video clock ↔ wall clock. Filename prior + OCR clock Theil–Sen fit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.ocr import read_ocr_parquet
from tradevidanalyser.schema import Alignment, AlignmentSample, SessionRecord

FILENAME_CONFIDENCE = 0.6
MANUAL_NO_SAMPLE_CONFIDENCE = 1.0
_ZERO_DX = 1e-12


@dataclass(frozen=True)
class _Measurement:
    video_t: float
    ocr_text: str
    parsed_wallclock: str
    y: float


def _r(value: float, ndigits: int) -> float:
    out = round(float(value), ndigits)
    return 0.0 if out == 0.0 else out


def _median(values: list[float]) -> float:
    if not values:
        raise ValueError("median of empty sequence")
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def theil_sen(xs: list[float], ys: list[float]) -> tuple[float, float]:
    """Theil–Sen fit of ``y = intercept + slope * x``.

    Returns ``(slope, intercept)``. Pairwise slopes skip identical ``x``.
    ``n == 1`` (or all ``x`` tied) uses slope 0 and intercept = median(y).
    """
    if len(xs) != len(ys):
        raise ValueError("theil_sen xs/ys length mismatch")
    if not xs:
        raise ValueError("theil_sen requires at least one sample")
    if len(xs) == 1:
        return 0.0, ys[0]
    slopes: list[float] = []
    for i in range(len(xs)):
        for j in range(i + 1, len(xs)):
            dx = xs[j] - xs[i]
            if abs(dx) < _ZERO_DX:
                continue
            slopes.append((ys[j] - ys[i]) / dx)
    if not slopes:
        return 0.0, _median(list(ys))
    slope = _median(slopes)
    intercept = _median([y - slope * x for x, y in zip(xs, ys, strict=True)])
    return slope, intercept


def confidence_from_residuals(residuals: list[float]) -> float:
    """0–1 score from sample count and residual MAD (seconds)."""
    n = len(residuals)
    if n == 0:
        return 0.0
    n_score = min(1.0, n / 10.0)
    centre = _median(list(residuals))
    mad = _median([abs(r - centre) for r in residuals])
    spread_score = max(0.0, 1.0 - mad / 2.0)
    confidence = n_score * spread_score
    if n < 3:
        confidence = min(confidence, 0.5)
    if n == 1:
        confidence = min(confidence, 0.35)
    return _r(confidence, 4)


def _parse_dt(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def _clock_measurements(root: Path, session_id: str, start: datetime) -> list[_Measurement]:
    path = store.ocr_path(root, session_id)
    if not path.is_file():
        return []
    out: list[_Measurement] = []
    for row in read_ocr_parquet(path):
        if row.roi != "clock" or not row.parsed:
            continue
        parsed = _parse_dt(row.parsed)
        if parsed is None:
            continue
        if (parsed.tzinfo is None) != (start.tzinfo is None):
            continue
        video_t = float(row.t)
        y = (parsed - start).total_seconds() - video_t
        out.append(
            _Measurement(
                video_t=video_t,
                ocr_text=row.text,
                parsed_wallclock=row.parsed,
                y=y,
            )
        )
    out.sort(key=lambda item: (item.video_t, item.ocr_text, item.parsed_wallclock))
    return out


def _samples_for(
    measurements: list[_Measurement],
    *,
    offset_s: float,
    drift_s_per_h: float,
) -> tuple[list[AlignmentSample], list[float]]:
    samples: list[AlignmentSample] = []
    residuals: list[float] = []
    for item in measurements:
        residual = item.y - (offset_s + drift_s_per_h * (item.video_t / 3600.0))
        residuals.append(residual)
        samples.append(
            AlignmentSample(
                video_t=_r(item.video_t, 3),
                ocr_text=item.ocr_text,
                parsed_wallclock=item.parsed_wallclock,
                residual_s=_r(residual, 6),
            )
        )
    return samples, residuals


def _filename_alignment() -> Alignment:
    return Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=FILENAME_CONFIDENCE,
        method="filename",
        samples=[],
    )


def _manual_alignment(manual_offset: float, measurements: list[_Measurement]) -> Alignment:
    offset_s = _r(manual_offset, 6)
    samples, residuals = _samples_for(measurements, offset_s=offset_s, drift_s_per_h=0.0)
    confidence = (
        confidence_from_residuals(residuals) if residuals else MANUAL_NO_SAMPLE_CONFIDENCE
    )
    return Alignment(
        offset_s=offset_s,
        drift_s_per_h=0.0,
        confidence=confidence,
        method="manual",
        samples=samples,
    )


def _ocr_alignment(measurements: list[_Measurement]) -> Alignment:
    xs = [item.video_t for item in measurements]
    ys = [item.y for item in measurements]
    slope, intercept = theil_sen(xs, ys)
    offset_s = _r(intercept, 6)
    drift_s_per_h = _r(slope * 3600.0, 6)
    samples, residuals = _samples_for(
        measurements, offset_s=offset_s, drift_s_per_h=drift_s_per_h
    )
    return Alignment(
        offset_s=offset_s,
        drift_s_per_h=drift_s_per_h,
        confidence=confidence_from_residuals(residuals),
        method="ocr_clock",
        samples=samples,
    )


def compute_alignment(
    record: SessionRecord,
    *,
    root: Path,
    manual_offset: float | None = None,
) -> Alignment:
    """Fit alignment from filename prior + ``ocr.parquet`` clock rows."""
    start = _parse_dt(record.recording.start_wallclock_vienna)
    measurements: list[_Measurement] = []
    if start is not None:
        measurements = _clock_measurements(root, record.id, start)
    if manual_offset is not None:
        return _manual_alignment(manual_offset, measurements)
    if not measurements:
        return _filename_alignment()
    return _ocr_alignment(measurements)


def align_session(
    session_id: str,
    *,
    root: Path,
    manual_offset: float | None = None,
) -> Alignment:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    alignment = compute_alignment(record, root=root, manual_offset=manual_offset)
    store.save_session(root, record.model_copy(update={"alignment": alignment}))
    store.compute_status(root, session_id)
    return alignment


def alignment_result_dict(session_id: str, alignment: Alignment) -> dict:
    payload = alignment.model_dump(mode="json")
    payload["session_id"] = session_id
    payload["status"] = "ok"
    return payload
