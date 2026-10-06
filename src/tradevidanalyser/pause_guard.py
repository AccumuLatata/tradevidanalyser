"""Pause detector → ``TVA_ROOT/pause_checks/<session_id>.json`` (PR-30, plan §2.2)."""

from __future__ import annotations

import math
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from tradevidanalyser import store
from tradevidanalyser.align import compute_alignment
from tradevidanalyser.frames import _extract_one, get_layout, media_for_time
from tradevidanalyser.media import MediaError
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.ocr import (
    FakeOcrProvider,
    clock_text_resolution,
    crop_roi,
    get_ocr_provider,
    parse_clock,
)
from tradevidanalyser.schema import Alignment, SessionRecord

SCHEMA_VERSION = "1"
AGREE_S = 5.0
PAUSE_CONFIRM_S = 30.0
PAUSE_DETECTABLE_FROM_S = 30.0
AGREE_S_HM = 60.0
PAUSE_CONFIRM_S_HM = 120.0
PAUSE_DETECTABLE_FROM_HM_S = 180.0

FAKE_PROVIDER_ERROR = "Guard braucht echten OCR-Provider"
VIDEO_UNREADABLE_ERROR = "Videodatei nicht lesbar; Guard kann nicht laufen"
FORCE_FLAG_OFF_ERROR = "--force gibt es nur, solange TVA_PAUSE_GUARD an ist"
MANUAL_SUSPECTED_ERROR = "Session ist suspected; --force erforderlich"
KEY_DURATION_TOL = 1e-6

PauseStatus = Literal["clear", "suspected", "unverifiable"]
ClockReader = Callable[[float], str | None]

_injected_reader: ClockReader | None = None


def set_clock_reader(reader: ClockReader | None) -> None:
    """Tests inject OCR clock text per video_t. Production leaves this None."""
    global _injected_reader
    _injected_reader = reader


def get_clock_reader() -> ClockReader | None:
    return _injected_reader


@dataclass
class Sample:
    video_t: float
    text: str
    parsed: str | None
    y: float | None
    resolution_s: int | None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "video_t": _r(self.video_t, 3),
            "text": self.text,
            "parsed": self.parsed,
            "y": None if self.y is None else _r(self.y, 6),
            "resolution_s": self.resolution_s,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


@dataclass
class PauseCheck:
    session_id: str
    recording_sha256: str
    part_shas: list[str]
    duration_s: float
    pause_check: PauseStatus
    pause_total_s: float | None
    clock_resolution_s: int | None
    pause_detectable_from_s: float | None
    ocr_provider: str
    ocr_model: str
    accepted_samples: list[Sample] = field(default_factory=list)
    rejected_samples: list[Sample] = field(default_factory=list)
    fit_before: dict[str, Any] | None = None
    override: Literal["force"] | None = None
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "recording_sha256": self.recording_sha256,
            "part_shas": list(self.part_shas),
            "duration_s": self.duration_s,
            "pause_check": self.pause_check,
            "pause_total_s": self.pause_total_s,
            "clock_resolution_s": self.clock_resolution_s,
            "pause_detectable_from_s": self.pause_detectable_from_s,
            "ocr_provider": self.ocr_provider,
            "ocr_model": self.ocr_model,
            "accepted_samples": [item.as_dict() for item in self.accepted_samples],
            "rejected_samples": [item.as_dict() for item in self.rejected_samples],
            "fit_before": self.fit_before,
            "override": self.override,
        }


def pause_checks_dir(root: Path) -> Path:
    return Path(root) / "pause_checks"


def pause_checks_path(root: Path, session_id: str) -> Path:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return pause_checks_dir(root) / f"{session_id}.json"


def recording_key(record: SessionRecord) -> tuple[str, tuple[str, ...], float]:
    sha = record.recording.sha256
    parts = tuple(part.sha256 for part in record.recording.parts)
    return sha, parts, float(record.recording.duration_s)


def key_matches(check: PauseCheck, record: SessionRecord) -> bool:
    sha, parts, duration = recording_key(record)
    if check.recording_sha256 != sha:
        return False
    if tuple(check.part_shas) != parts:
        return False
    return math.isclose(float(check.duration_s), duration, abs_tol=KEY_DURATION_TOL)


def load_pause_check(
    root: Path,
    session_id: str,
    *,
    allow_fake: bool = False,
) -> PauseCheck | None:
    path = pause_checks_path(root, session_id)
    if not path.is_file():
        return None
    try:
        raw = store.read_json(path)
    except (OSError, ValueError):
        return None
    check = _from_payload(raw)
    if check is None:
        return None
    if check.ocr_provider == "fake" and not allow_fake:
        return None
    return check


def write_pause_check(root: Path, check: PauseCheck) -> Path:
    path = pause_checks_path(root, check.session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(path, check.as_dict())
    return path


def delete_pause_checks(root: Path, session_id: str | None = None, *, all_sessions: bool = False) -> list[str]:
    """Named rollback: delete pause_checks files. Does not rewrite alignment."""
    deleted: list[str] = []
    if all_sessions:
        folder = pause_checks_dir(root)
        if folder.is_dir():
            for path in sorted(folder.glob("*.json")):
                path.unlink(missing_ok=True)
                deleted.append(path.stem)
        return deleted
    if not session_id:
        raise ValueError("pause-checks delete requires a session id or --all")
    path = pause_checks_path(root, session_id)
    if path.is_file():
        path.unlink()
        deleted.append(session_id)
    return deleted


def sample_times(duration: float) -> list[float]:
    if not math.isfinite(duration) or duration < 0:
        return []
    inset = min(1.0, duration / 4.0)
    span = max(duration - 2.0 * inset, 0.0)
    return [inset + i * span / 7.0 for i in range(8)]


def detect_pauses(
    record: SessionRecord,
    *,
    root: Path,
    reader: ClockReader | None = None,
) -> PauseCheck:
    """Sample clocks and decide clear / suspected / unverifiable. Does not write."""
    sha, parts, duration = recording_key(record)
    start = _parse_start(record.recording.start_wallclock_vienna)
    provider_name, provider_model = _provider_label(reader)
    times = sample_times(duration)
    accepted: list[Sample] = []
    rejected: list[Sample] = []
    pending: list[tuple[Sample, list[Sample]]] = []

    for t_i in times:
        center, neighbors = _read_group(record, root, start, t_i, duration, reader)
        if center.y is None or center.resolution_s is None:
            center.reason = center.reason or "unreadable"
            rejected.append(center)
            continue
        agree = _agree_s(center.resolution_s)
        if _confirmed(center, neighbors, agree):
            pending.append((center, neighbors))
        else:
            center.reason = "unconfirmed"
            rejected.append(center)

    res1 = [item for item, _nb in pending if item.resolution_s == 1]
    res60 = [item for item, _nb in pending if item.resolution_s == 60]
    if len(res1) >= 2:
        file_res: int | None = 1
    elif len(res60) >= 2:
        file_res = 60
    else:
        file_res = None

    for center, neighbors in pending:
        if file_res == 1 and center.resolution_s == 60:
            center.reason = "resolution_mismatch"
            rejected.append(center)
            continue
        if file_res == 60 and center.resolution_s == 1:
            _truncate_to_minute(center, start)
        accepted.append(center)

    if file_res is None or len(accepted) < 2:
        accepted.sort(key=lambda item: item.video_t)
        return PauseCheck(
            session_id=record.id,
            recording_sha256=sha,
            part_shas=list(parts),
            duration_s=duration,
            pause_check="unverifiable",
            pause_total_s=None,
            clock_resolution_s=None,
            pause_detectable_from_s=None,
            ocr_provider=provider_name,
            ocr_model=provider_model,
            accepted_samples=accepted,
            rejected_samples=rejected,
        )

    agree, confirm, detectable = _thresholds(file_res)
    accepted, extra_rejected = _drop_outliers(
        record, root, start, duration, accepted, agree, reader
    )
    rejected.extend(extra_rejected)

    if len(accepted) < 2:
        return PauseCheck(
            session_id=record.id,
            recording_sha256=sha,
            part_shas=list(parts),
            duration_s=duration,
            pause_check="unverifiable",
            pause_total_s=None,
            clock_resolution_s=file_res,
            pause_detectable_from_s=detectable,
            ocr_provider=provider_name,
            ocr_model=provider_model,
            accepted_samples=accepted,
            rejected_samples=rejected,
        )

    accepted.sort(key=lambda item: item.video_t)
    for prev, nxt in zip(accepted, accepted[1:], strict=False):
        if prev.y is None or nxt.y is None:
            continue
        if nxt.y < prev.y - agree:
            return PauseCheck(
                session_id=record.id,
                recording_sha256=sha,
                part_shas=list(parts),
                duration_s=duration,
                pause_check="unverifiable",
                pause_total_s=_r((accepted[-1].y or 0.0) - (accepted[0].y or 0.0), 6),
                clock_resolution_s=file_res,
                pause_detectable_from_s=detectable,
                ocr_provider=provider_name,
                ocr_model=provider_model,
                accepted_samples=accepted,
                rejected_samples=rejected,
            )

    y_first = accepted[0].y
    y_last = accepted[-1].y
    assert y_first is not None and y_last is not None
    pause_total = y_last - y_first
    status: PauseStatus = "suspected" if pause_total > confirm else "clear"
    return PauseCheck(
        session_id=record.id,
        recording_sha256=sha,
        part_shas=list(parts),
        duration_s=duration,
        pause_check=status,
        pause_total_s=_r(pause_total, 6),
        clock_resolution_s=file_res,
        pause_detectable_from_s=detectable,
        ocr_provider=provider_name,
        ocr_model=provider_model,
        accepted_samples=accepted,
        rejected_samples=rejected,
    )


def run_pause_guard(
    record: SessionRecord,
    *,
    root: Path,
    reader: ClockReader | None = None,
    apply_alignment: bool = True,
    override: Literal["force"] | None = None,
) -> PauseCheck:
    """Write pause_checks. Optionally set session.alignment to invalid on suspected."""
    _require_can_sample(record, root, reader=reader)
    check = detect_pauses(record, root=root, reader=reader or get_clock_reader())
    check.fit_before = _fit_before(record, root)
    check.override = override
    write_pause_check(root, check)
    if apply_alignment and check.pause_check == "suspected" and override != "force":
        _write_invalid_alignment(root, record)
    return check


def ensure_pause_check(
    record: SessionRecord,
    *,
    root: Path,
    reader: ClockReader | None = None,
    apply_alignment: bool = True,
) -> PauseCheck:
    """Load a matching pause_checks file, or run the guard / abort (plan §2.2)."""
    loaded = load_pause_check(root, record.id)
    if loaded is not None and key_matches(loaded, record):
        return loaded
    if not _video_readable(record, root) and (reader or get_clock_reader()) is None:
        _require_can_sample(record, root, reader=reader)
        raise ValueError(VIDEO_UNREADABLE_ERROR)
    return run_pause_guard(
        record, root=root, reader=reader, apply_alignment=apply_alignment
    )


def sample_clocks_readonly(
    record: SessionRecord,
    *,
    root: Path,
    reader: ClockReader | None = None,
) -> dict[str, Any]:
    """Same detector as the guard, no session or pause_checks write (audit --sample-clocks)."""
    try:
        _require_can_sample(record, root, reader=reader)
        check = detect_pauses(record, root=root, reader=reader or get_clock_reader())
        check.fit_before = _fit_before(record, root)
        return check.as_dict()
    except (ValueError, OSError, MediaError) as exc:
        return {"session_id": record.id, "error": str(exc)}


def _require_can_sample(
    record: SessionRecord,
    root: Path,
    *,
    reader: ClockReader | None,
) -> None:
    if reader is not None or get_clock_reader() is not None:
        return
    provider = get_ocr_provider()
    if isinstance(provider, FakeOcrProvider) or provider.name == "fake":
        raise ValueError(FAKE_PROVIDER_ERROR)
    if not _video_readable(record, root):
        raise ValueError(VIDEO_UNREADABLE_ERROR)


def _video_readable(record: SessionRecord, root: Path) -> bool:
    try:
        path, _seek = media_for_time(record, root, 0.0)
    except (ValueError, OSError, FileNotFoundError):
        return False
    return path.is_file()


def _provider_label(reader: ClockReader | None) -> tuple[str, str]:
    if reader is not None or get_clock_reader() is not None:
        return "injected", "injected-v1"
    provider = get_ocr_provider()
    return provider.name, provider.model


def _fit_before(record: SessionRecord, root: Path) -> dict[str, Any] | None:
    if record.alignment is not None:
        return {
            "method": record.alignment.method,
            "offset_s": record.alignment.offset_s,
            "drift_s_per_h": record.alignment.drift_s_per_h,
            "confidence": record.alignment.confidence,
        }
    try:
        fit = compute_alignment(record, root=root)
    except (ValueError, OSError):
        return None
    return {
        "method": fit.method,
        "offset_s": fit.offset_s,
        "drift_s_per_h": fit.drift_s_per_h,
        "confidence": fit.confidence,
    }


def _write_invalid_alignment(root: Path, record: SessionRecord) -> None:
    alignment = Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=0.0,
        method="invalid",
        samples=[],
    )
    store.save_session(root, record.model_copy(update={"alignment": alignment}))
    store.evidence_path(root, record.id).unlink(missing_ok=True)
    store.drop_debrief(root, record.id)
    store.drop_proposals(root, record.id)
    store.drop_ledger_session(root, record.id)
    store.compute_status(root, record.id)


def _parse_start(text: str) -> datetime:
    start = datetime.fromisoformat(text)
    if start.tzinfo is None:
        return start.replace(tzinfo=VIENNA)
    return start


def _r(value: float, ndigits: int) -> float:
    out = round(float(value), ndigits)
    return 0.0 if out == 0.0 else out


def _agree_s(resolution_s: int) -> float:
    return AGREE_S if resolution_s == 1 else AGREE_S_HM


def _thresholds(file_res: int) -> tuple[float, float, float]:
    if file_res == 1:
        return AGREE_S, PAUSE_CONFIRM_S, PAUSE_DETECTABLE_FROM_S
    return AGREE_S_HM, PAUSE_CONFIRM_S_HM, PAUSE_DETECTABLE_FROM_HM_S


def _clamp(t: float, duration: float) -> float:
    if not math.isfinite(duration) or duration <= 0:
        return 0.0
    return min(max(t, 0.0), duration)


def _read_group(
    record: SessionRecord,
    root: Path,
    start: datetime,
    t_i: float,
    duration: float,
    reader: ClockReader | None,
) -> tuple[Sample, list[Sample]]:
    left = _clamp(t_i - 2.0, duration)
    right = _clamp(t_i + 2.0, duration)
    center = _read_at(record, root, start, t_i, reader)
    neighbors = []
    if abs(left - t_i) > 1e-9:
        neighbors.append(_read_at(record, root, start, left, reader))
    if abs(right - t_i) > 1e-9 and abs(right - left) > 1e-9:
        neighbors.append(_read_at(record, root, start, right, reader))
    return center, neighbors


def _read_at(
    record: SessionRecord,
    root: Path,
    start: datetime,
    video_t: float,
    reader: ClockReader | None,
) -> Sample:
    text = _clock_text(record, root, video_t, reader)
    if not text:
        return Sample(video_t=video_t, text="", parsed=None, y=None, resolution_s=None)
    resolution = clock_text_resolution(text)
    parsed_s = parse_clock(text, prior=start, at_s=video_t)
    if parsed_s is None or resolution is None:
        return Sample(
            video_t=video_t,
            text=text,
            parsed=parsed_s,
            y=None,
            resolution_s=resolution,
            reason="unparseable",
        )
    parsed = datetime.fromisoformat(parsed_s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=start.tzinfo)
    y = (parsed - start).total_seconds() - video_t
    return Sample(
        video_t=video_t,
        text=text,
        parsed=parsed.isoformat(),
        y=y,
        resolution_s=resolution,
    )


def _clock_text(
    record: SessionRecord,
    root: Path,
    video_t: float,
    reader: ClockReader | None,
) -> str:
    injected = reader or get_clock_reader()
    if injected is not None:
        return (injected(video_t) or "").strip()
    return _ocr_clock_text(record, root, video_t)


def _ocr_clock_text(record: SessionRecord, root: Path, video_t: float) -> str:
    try:
        media_path, seek_s = media_for_time(record, root, video_t)
        layout = get_layout(root=root)
        roi = layout.rois.get("clock")
        if roi is None:
            return ""
        provider = get_ocr_provider()
        with tempfile.TemporaryDirectory(prefix="tva-pause-") as tmp:
            tmp_dir = Path(tmp)
            frame = tmp_dir / "frame.jpg"
            _extract_one(media_path, seek_s, frame)
            crop = tmp_dir / "clock.jpg"
            crop_roi(frame, roi, crop)
            read = provider.read(crop, roi="clock")
            return (read.text or "").strip()
    except (OSError, ValueError, MediaError, FileNotFoundError):
        return ""


def _confirmed(center: Sample, neighbors: list[Sample], agree: float) -> bool:
    if center.y is None:
        return False
    for item in neighbors:
        if item.y is None:
            continue
        if abs(item.y - center.y) <= agree:
            return True
    return False


def _truncate_to_minute(sample: Sample, start: datetime) -> None:
    if sample.parsed is None or sample.y is None:
        return
    parsed = datetime.fromisoformat(sample.parsed)
    parsed = parsed.replace(second=0, microsecond=0)
    sample.parsed = parsed.isoformat()
    sample.y = (parsed - start).total_seconds() - sample.video_t
    sample.resolution_s = 60


def _drop_outliers(
    record: SessionRecord,
    root: Path,
    start: datetime,
    duration: float,
    accepted: list[Sample],
    agree: float,
    reader: ClockReader | None,
) -> tuple[list[Sample], list[Sample]]:
    accepted = sorted(accepted, key=lambda item: item.video_t)
    if len(accepted) < 2:
        return accepted, []
    rejected: list[Sample] = []
    keep = list(accepted)
    outliers = _outlier_indices(keep, agree)
    for index in outliers:
        sample = keep[index]
        sample.reason = "outlier"
        replacement = _reread_other_neighbor(
            record, root, start, duration, sample, keep, agree, reader
        )
        rejected.append(sample)
        keep[index] = replacement
    kept: list[Sample] = []
    for item in keep:
        if item is None or item.reason == "outlier":
            continue
        if item.reason in {"outlier_reread", "unconfirmed"}:
            rejected.append(item)
            continue
        kept.append(item)
    # drop any replacement that is still an outlier vs the surviving ends
    if len(kept) >= 2:
        still_bad = set(_outlier_indices(kept, agree))
        if still_bad:
            cleaned: list[Sample] = []
            for i, item in enumerate(kept):
                if i in still_bad:
                    item.reason = item.reason or "outlier"
                    rejected.append(item)
                else:
                    cleaned.append(item)
            kept = cleaned
    return kept, rejected


def _outlier_indices(accepted: list[Sample], agree: float) -> list[int]:
    ys = [item.y for item in accepted]
    if any(y is None for y in ys) or len(accepted) < 2:
        return []
    values = [float(y) for y in ys if y is not None]
    out: list[int] = []
    later = values[1:]
    if later and all(values[0] > y + agree for y in later):
        out.append(0)
    earlier = values[:-1]
    if earlier and all(values[-1] < y - agree for y in earlier):
        out.append(len(accepted) - 1)
    y_first, y_last = values[0], values[-1]
    lo, hi = min(y_first, y_last), max(y_first, y_last)
    for i, y in enumerate(values[1:-1], start=1):
        if y < lo - agree or y > hi + agree:
            out.append(i)
    return out


def _reread_other_neighbor(
    record: SessionRecord,
    root: Path,
    start: datetime,
    duration: float,
    sample: Sample,
    accepted: list[Sample],
    agree: float,
    reader: ClockReader | None,
) -> Sample:
    left = _clamp(sample.video_t - 2.0, duration)
    right = _clamp(sample.video_t + 2.0, duration)
    other = right if abs(right - sample.video_t) >= abs(left - sample.video_t) else left
    if abs(other - sample.video_t) <= 1e-9:
        sample.reason = "outlier"
        return sample
    new = _read_at(record, root, start, other, reader)
    neighbors = [_read_at(record, root, start, sample.video_t, reader)]
    alt = left if other == right else right
    if abs(alt - other) > 1e-9 and abs(alt - sample.video_t) > 1e-9:
        neighbors.append(_read_at(record, root, start, alt, reader))
    if new.y is None or new.resolution_s is None or not _confirmed(new, neighbors, agree):
        new.reason = new.reason or "unconfirmed"
        return new
    ys = [item.y for item in accepted if item.y is not None and item is not sample]
    if len(ys) >= 1:
        y_first, y_last = ys[0], ys[-1]
        lo, hi = min(y_first, y_last), max(y_first, y_last)
        if new.y < lo - agree or new.y > hi + agree:
            new.reason = "outlier_reread"
            return new
    return new


def _from_payload(raw: dict[str, Any]) -> PauseCheck | None:
    try:
        accepted = [_sample_from_payload(item) for item in raw.get("accepted_samples") or []]
        rejected = [_sample_from_payload(item) for item in raw.get("rejected_samples") or []]
        override = raw.get("override")
        if override not in {None, "force"}:
            override = None
        status = raw.get("pause_check")
        if status not in {"clear", "suspected", "unverifiable"}:
            return None
        return PauseCheck(
            session_id=str(raw["session_id"]),
            recording_sha256=str(raw["recording_sha256"]),
            part_shas=[str(item) for item in raw.get("part_shas") or []],
            duration_s=float(raw["duration_s"]),
            pause_check=status,
            pause_total_s=_opt_float(raw.get("pause_total_s")),
            clock_resolution_s=_opt_int(raw.get("clock_resolution_s")),
            pause_detectable_from_s=_opt_float(raw.get("pause_detectable_from_s")),
            ocr_provider=str(raw.get("ocr_provider") or ""),
            ocr_model=str(raw.get("ocr_model") or ""),
            accepted_samples=accepted,
            rejected_samples=rejected,
            fit_before=raw.get("fit_before") if isinstance(raw.get("fit_before"), dict) else None,
            override=override,
            schema_version=str(raw.get("schema_version") or SCHEMA_VERSION),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _sample_from_payload(raw: object) -> Sample:
    if not isinstance(raw, dict):
        return Sample(video_t=0.0, text="", parsed=None, y=None, resolution_s=None)
    return Sample(
        video_t=float(raw.get("video_t") or 0.0),
        text=str(raw.get("text") or ""),
        parsed=raw.get("parsed") if raw.get("parsed") is not None else None,
        y=_opt_float(raw.get("y")),
        resolution_s=_opt_int(raw.get("resolution_s")),
        reason=str(raw["reason"]) if raw.get("reason") else None,
    )


def _opt_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _opt_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
