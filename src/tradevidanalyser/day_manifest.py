"""Day manifest, fingerprint, and lock (PR-31, plan §2.1)."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from tradevidanalyser import __version__, media, store
from tradevidanalyser.day_audit import FillIdentity, fill_identity
from tradevidanalyser.fills import load_fills, venue_from_hint
from tradevidanalyser.fills_mirror import FillRecord
from tradevidanalyser.flags import (
    active_flag_names,
    day_manifest_enabled,
    pause_guard_enabled,
)
from tradevidanalyser.naming import VIENNA, vienna_today
from tradevidanalyser.pause_guard import key_matches, load_pause_check
from tradevidanalyser.schema import SessionRecord
from tradevidanalyser.watch import release_lock, try_acquire_lock

SCHEMA_VERSION = "1"
DAY_STALE_ERROR = "Tag veraltet, tva day build ausführen"
MANIFEST_OFF_ERROR = "TVA_DAY_MANIFEST ist aus"
PAUSE_KEY_ERROR = "pause_checks fehlt oder Schlüssel passt nicht"
LOCK_ERROR = "Tag ist gesperrt"
TZ_ASSUMPTION_CSV = "csv_offset_as_is"
ENABLED_FROM_NAME = "enabled_from"
CURRENT_NAME = "current"
LOCK_NAME = "lock"

DayKind = Literal[
    "legacy",
    "legacy_unbuilt",
    "current",
    "current_incomplete",
    "stale",
    "missing",
]


class DayStale(ValueError):
    """Day-path fingerprint is stale or the pointer is missing (plan §2.1)."""

    def __init__(self, dates: list[date], *, text: str = DAY_STALE_ERROR) -> None:
        self.dates = list(dates)
        listed = ", ".join(item.isoformat() for item in self.dates)
        super().__init__(f"{text}: {listed}" if listed else text)


class DayManifestError(ValueError):
    pass


@dataclass(frozen=True)
class IncompleteSession:
    session_id: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"session_id": self.session_id, "reason": self.reason}


@dataclass
class DayState:
    kind: DayKind
    date: date
    incomplete: list[IncompleteSession] = field(default_factory=list)
    build_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "day_state": self.kind,
            "date": self.date.isoformat(),
        }
        if self.incomplete:
            payload["incomplete"] = [item.as_dict() for item in self.incomplete]
        if self.build_id:
            payload["build_id"] = self.build_id
        return payload


@dataclass
class DayBuildResult:
    date: date
    status: str
    path: str | None = None
    build_id: str | None = None
    written: bool = False
    clips: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "date": self.date.isoformat(),
            "written": self.written,
            "clips": self.clips,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.build_id is not None:
            payload["build_id"] = self.build_id
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def days_dir(root: Path) -> Path:
    return Path(root) / "days"


def enabled_from_path(root: Path) -> Path:
    return days_dir(root) / ENABLED_FROM_NAME


def day_dir(root: Path, day: date) -> Path:
    return days_dir(root) / day.isoformat()


def current_pointer_path(root: Path, day: date) -> Path:
    return day_dir(root, day) / CURRENT_NAME


def day_lock_path(root: Path, day: date) -> Path:
    return day_dir(root, day) / LOCK_NAME


def read_enabled_from(root: Path) -> date | None:
    path = enabled_from_path(root)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()[0]
        return date.fromisoformat(text)
    except (OSError, ValueError, IndexError):
        return None


def write_enabled_from(root: Path, day: date) -> Path:
    """Writer-only. Readers must not call this (plan §2.1 / §3.4)."""
    path = enabled_from_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{ENABLED_FROM_NAME}.{os.getpid()}.tmp")
    tmp.write_text(day.isoformat() + "\n", encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return path


def write_enabled_from_if_absent(root: Path, day: date) -> date | None:
    """First writer wins. Does not overwrite an existing enabled_from."""
    path = enabled_from_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(os.fspath(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return read_enabled_from(root)
    try:
        os.write(fd, (day.isoformat() + "\n").encode("utf-8"))
        os.fsync(fd)
    except OSError:
        os.close(fd)
        path.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    return day


def enable_day_manifest(root: Path, day: date | None = None) -> date:
    if not day_manifest_enabled():
        raise DayManifestError(MANIFEST_OFF_ERROR)
    chosen = day or vienna_today()
    write_enabled_from(root, chosen)
    return chosen


def delete_day_manifest(
    root: Path,
    day: date | None = None,
    *,
    all_days: bool = False,
) -> list[str]:
    """Named rollback: delete day folders / enabled_from. Keeps days/audit.json."""
    deleted: list[str] = []
    if all_days:
        folder = days_dir(root)
        if folder.is_dir():
            for child in sorted(folder.iterdir()):
                if child.name == "audit.json":
                    continue
                if child.is_dir() and _is_iso_date(child.name):
                    _rmtree(child)
                    deleted.append(child.name)
                elif child.name == ENABLED_FROM_NAME and child.is_file():
                    child.unlink()
                    deleted.append(ENABLED_FROM_NAME)
        return deleted
    if day is None:
        raise DayManifestError("day delete requires a date or --all")
    folder = day_dir(root, day)
    if folder.is_dir():
        _rmtree(folder)
        deleted.append(day.isoformat())
    return deleted


def day_state(root: Path, day: date) -> DayState:
    """Return the day-path state. Does not write. Flag off is always legacy."""
    if not day_manifest_enabled():
        return DayState(kind="legacy", date=day)
    enabled = read_enabled_from(root)
    pointer = _read_current_build_id(root, day)
    if enabled is None or (day < enabled and pointer is None):
        return DayState(kind="legacy_unbuilt", date=day)
    clips = clips_for_day(root, day)
    if not is_day_path(root, clips):
        return DayState(kind="legacy", date=day)
    payload = load_current_day_json(root, day)
    if payload is None or pointer is None:
        return DayState(kind="missing", date=day)
    if not fingerprint_matches(root, day, payload, clips):
        return DayState(kind="stale", date=day, build_id=pointer)
    incomplete = incomplete_sessions(root, clips)
    if incomplete:
        return DayState(
            kind="current_incomplete",
            date=day,
            incomplete=incomplete,
            build_id=pointer,
        )
    return DayState(kind="current", date=day, build_id=pointer)


def require_fresh(root: Path, day: date) -> DayState:
    state = day_state(root, day)
    if state.kind in {"stale", "missing"}:
        raise DayStale([day])
    return state


def require_fresh_for_session(root: Path, session_id: str) -> DayState | None:
    if not day_manifest_enabled():
        return None
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    if not store.session_json_path(root, session_id).is_file():
        return None
    record = store.load_session(root, session_id)
    return require_fresh(root, nominal_vienna_date(record))


def require_fresh_for_dates(root: Path, days: list[date]) -> list[DayState]:
    states = [day_state(root, day) for day in unique_dates(days)]
    stale = [item.date for item in states if item.kind in {"stale", "missing"}]
    if stale:
        raise DayStale(stale)
    return states


def scan_store_days(root: Path) -> list[DayState]:
    if not day_manifest_enabled():
        return []
    return [day_state(root, day) for day in sorted(_session_days(root))]


def require_fresh_store(root: Path) -> list[DayState]:
    if not day_manifest_enabled():
        return []
    states = scan_store_days(root)
    stale = [item.date for item in states if item.kind in {"stale", "missing"}]
    if stale:
        raise DayStale(stale)
    return states


def incomplete_days(states: list[DayState]) -> list[dict[str, Any]]:
    return [
        {
            "date": item.date.isoformat(),
            "sessions": [row.as_dict() for row in item.incomplete],
        }
        for item in states
        if item.kind == "current_incomplete"
    ]


def legacy_unbuilt_days(states: list[DayState]) -> list[str]:
    return [item.date.isoformat() for item in states if item.kind == "legacy_unbuilt"]


def clips_for_day(root: Path, day: date) -> list[SessionRecord]:
    found: list[SessionRecord] = []
    for session_id in store.list_session_ids(root):
        record = store.load_session(root, session_id)
        if nominal_vienna_date(record) == day:
            found.append(record)
    found.sort(key=lambda rec: (nominal_start_utc(rec), rec.id))
    return found


def is_day_path(root: Path, clips: list[SessionRecord]) -> bool:
    if len(clips) >= 2:
        return True
    if not pause_guard_enabled():
        return False
    for record in clips:
        check = load_pause_check(root, record.id)
        if check is not None and key_matches(check, record) and check.pause_check == "suspected":
            return True
    return False


def nominal_vienna_date(record: SessionRecord) -> date:
    start = _aware_start(record)
    return start.astimezone(VIENNA).date()


def nominal_start_utc(record: SessionRecord) -> datetime:
    return _aware_start(record).astimezone(timezone.utc)


def nominal_end_utc(record: SessionRecord) -> datetime:
    return nominal_start_utc(record) + timedelta(seconds=float(record.recording.duration_s))


def load_current_day_json(root: Path, day: date) -> dict[str, Any] | None:
    build_id = _read_current_build_id(root, day)
    if build_id is None:
        return None
    path = day_dir(root, day) / "builds" / build_id / "day.json"
    if not path.is_file():
        return None
    try:
        payload = store.read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def fingerprint_matches(
    root: Path,
    day: date,
    payload: dict[str, Any],
    clips: list[SessionRecord] | None = None,
) -> bool:
    stored = payload.get("input_fingerprint")
    inputs = payload.get("fingerprint_inputs")
    if not isinstance(stored, str) or not isinstance(inputs, dict):
        return False
    clips = clips if clips is not None else clips_for_day(root, day)
    recomputed = _fingerprint_payload(
        clips,
        fill_identities=_fill_identities_from_stored(inputs.get("fill_identities")),
        venue=str(inputs.get("venue") or "unknown"),
        include_manual=bool(inputs.get("include_manual")),
        reconcile_dir=str(inputs.get("reconcile_dir") or ""),
        provider=str(inputs.get("provider") or ""),
        flags=active_flag_names(),
        schema_version=SCHEMA_VERSION,
        app_version=__version__,
    )
    return _canonical_sha256(recomputed) == stored


def incomplete_sessions(root: Path, clips: list[SessionRecord]) -> list[IncompleteSession]:
    out: list[IncompleteSession] = []
    for record in clips:
        has_evidence = store.evidence_path(root, record.id).is_file()
        has_debrief = store.debrief_json_path(root, record.id).is_file()
        if has_evidence and has_debrief:
            continue
        reason = "evidence_pending"
        check = load_pause_check(root, record.id) if pause_guard_enabled() else None
        if (
            check is not None
            and key_matches(check, record)
            and check.pause_check == "suspected"
            and not has_evidence
        ):
            reason = "alignment_invalid"
        out.append(IncompleteSession(session_id=record.id, reason=reason))
    return out


def build_day(
    root: Path,
    day: date,
    *,
    executions: Path,
    venue: str | None = None,
    include_manual: bool = False,
    reconcile_dir: Path | None = None,
    provider: str | None = None,
) -> DayBuildResult:
    if not day_manifest_enabled():
        raise DayManifestError(MANIFEST_OFF_ERROR)
    csv_path = Path(executions)
    if not csv_path.is_file():
        raise DayManifestError(f"executions file not found: {csv_path}")
    if read_enabled_from(root) is None:
        write_enabled_from_if_absent(root, day)
    clips = clips_for_day(root, day)
    if not is_day_path(root, clips):
        return DayBuildResult(
            date=day,
            status="legacy",
            written=False,
            clips=len(clips),
            reason="single clip without suspected",
        )
    lock = day_lock_path(root, day)
    if not try_acquire_lock(lock):
        raise DayManifestError(LOCK_ERROR)
    try:
        if pause_guard_enabled():
            for record in clips:
                check = load_pause_check(root, record.id)
                if check is None or not key_matches(check, record):
                    raise DayManifestError(f"{PAUSE_KEY_ERROR}: {record.id}")
        chosen_venue = venue_from_hint(csv_path, venue)
        try:
            loader, fills = load_fills(csv_path)
        except Exception as exc:
            if type(exc).__name__ == "JournalIngestError":
                raise DayManifestError(str(exc)) from exc
            raise
        identities = csv_window_fill_identities(fills, day)
        identities.sort(key=_identity_sort)
        provider_name = (provider or "").strip()
        reconcile_text = str(reconcile_dir) if reconcile_dir is not None else ""
        flags = active_flag_names()
        fp_inputs = _fingerprint_payload(
            clips,
            fill_identities=[item.as_dict() for item in identities],
            venue=chosen_venue,
            include_manual=include_manual,
            reconcile_dir=reconcile_text,
            provider=provider_name,
            flags=flags,
            schema_version=SCHEMA_VERSION,
            app_version=__version__,
        )
        fingerprint = _canonical_sha256(fp_inputs)
        previous = load_current_day_json(root, day)
        clip_rows = [_clip_row(root, record) for record in clips]
        outside: list[dict[str, Any]] = []
        trades_outside = 0
        ownership_store: dict[str, Any] = {}
        from tradevidanalyser.flags import exclusive_fills_enabled

        if exclusive_fills_enabled():
            from tradevidanalyser.day_fills import (
                apply_exclusive_build,
                assign_day_ownership,
                tz_assumption_for_build,
            )

            ownership = assign_day_ownership(
                root,
                day,
                fills,
                include_manual=include_manual,
                loader=loader,
            )
            build_id = new_ulid()
            dest = day_dir(root, day) / "builds" / build_id / "day.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            built = apply_exclusive_build(
                root,
                day,
                ownership,
                venue=chosen_venue,
                previous=previous,
                build_dir=dest.parent,
            )
            for row in clip_rows:
                session_id = str(row["session_id"])
                row["cascade"] = built.cascade.get(session_id, "unchanged")
                row["overlap"] = ownership.overlap.get(session_id, [])
                row["near_boundary"] = ownership.near_boundary.get(session_id, [])
            outside = [item.as_dict() for item in ownership.outside]
            trades_outside = sum(
                1
                for trade in ownership.trades
                if ownership.fill_owner.get(trade.entry_fill_id) is None
            )
            ownership_store = ownership.as_store()
            tz_assumption = tz_assumption_for_build(root)
            claimed_legacy = [item.as_dict() for item in ownership.claimed_legacy]
            proposal_remaps = list(built.remaps)
            proposals_discarded = list(built.discarded)
        else:
            build_id = new_ulid()
            dest = day_dir(root, day) / "builds" / build_id / "day.json"
            dest.parent.mkdir(parents=True, exist_ok=True)
            tz_assumption = TZ_ASSUMPTION_CSV
            claimed_legacy = []
            proposal_remaps = []
            proposals_discarded = []
        payload = {
            "schema_version": SCHEMA_VERSION,
            "date": day.isoformat(),
            "tz_assumption": tz_assumption,
            "executions_sha256": media.sha256_file(csv_path),
            "input_fingerprint": fingerprint,
            "fingerprint_inputs": fp_inputs,
            "clips": clip_rows,
            "outside": outside,
            "trades_outside_clips": trades_outside,
            "ownership": ownership_store,
        }
        if exclusive_fills_enabled():
            payload["claimed_by_legacy_neighbor"] = claimed_legacy
            payload["proposal_remaps"] = proposal_remaps
            payload["proposals_discarded"] = proposals_discarded
        store.write_json(dest, payload)
        _replace_current(root, day, build_id)
        if exclusive_fills_enabled():
            for record in clips:
                store.compute_status(root, record.id)
        return DayBuildResult(
            date=day,
            status="ok",
            path=str(dest.relative_to(root)),
            build_id=build_id,
            written=True,
            clips=len(clips),
        )
    finally:
        release_lock(lock)


def new_ulid() -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    timestamp_ms = int(time.time() * 1000)
    value = (timestamp_ms << 80) | secrets.randbits(80)
    chars = ["0"] * 26
    for index in range(25, -1, -1):
        chars[index] = alphabet[value & 31]
        value >>= 5
    return "".join(chars)


def _clip_row(root: Path, record: SessionRecord) -> dict[str, Any]:
    start_utc = nominal_start_utc(record)
    end_utc = nominal_end_utc(record)
    alignment = _alignment_fields(root, record)
    pause = _pause_fields(root, record)
    return {
        "session_id": record.id,
        "filename": record.recording.filename or Path(record.recording.path).name,
        "nominal_start": start_utc.astimezone(VIENNA).isoformat(),
        "nominal_end": end_utc.astimezone(VIENNA).isoformat(),
        "duration_s": float(record.recording.duration_s),
        "stitched_gap_s": _stitched_gap_s(record),
        **alignment,
        **pause,
        "cascade": "unchanged",
        "overlap": [],
        "near_boundary": [],
    }


def _alignment_fields(root: Path, record: SessionRecord) -> dict[str, Any]:
    check = load_pause_check(root, record.id) if pause_guard_enabled() else None
    fit: dict[str, Any] | None = None
    if check is not None and key_matches(check, record) and isinstance(check.fit_before, dict):
        fit = check.fit_before
    elif record.alignment is not None:
        fit = {
            "method": record.alignment.method,
            "offset_s": record.alignment.offset_s,
            "drift_s_per_h": record.alignment.drift_s_per_h,
            "confidence": record.alignment.confidence,
        }
    if fit is None:
        return {
            "alignment_method": None,
            "alignment_confidence": None,
            "alignment_offset_s": None,
            "alignment_drift_s_per_h": None,
        }
    return {
        "alignment_method": fit.get("method"),
        "alignment_confidence": fit.get("confidence"),
        "alignment_offset_s": fit.get("offset_s"),
        "alignment_drift_s_per_h": fit.get("drift_s_per_h"),
    }


def _pause_fields(root: Path, record: SessionRecord) -> dict[str, Any]:
    if not pause_guard_enabled():
        return {
            "pause_check": "guard_off",
            "pause_total_s": None,
            "clock_resolution_s": None,
            "pause_detectable_from_s": None,
        }
    check = load_pause_check(root, record.id)
    if check is None or not key_matches(check, record):
        raise DayManifestError(f"{PAUSE_KEY_ERROR}: {record.id}")
    return {
        "pause_check": check.pause_check,
        "pause_total_s": check.pause_total_s,
        "clock_resolution_s": check.clock_resolution_s,
        "pause_detectable_from_s": check.pause_detectable_from_s,
    }


def _stitched_gap_s(record: SessionRecord) -> float | None:
    from tradevidanalyser.naming import FilenameError, parse_obs_filename

    parts = list(record.recording.parts or [])
    if len(parts) < 2:
        return None
    timed: list[tuple[datetime, float]] = []
    for part in parts:
        name = part.filename or Path(part.path).name
        try:
            _, start = parse_obs_filename(name)
        except FilenameError:
            continue
        timed.append((start, float(part.duration_s)))
    if len(timed) < 2:
        return None
    timed.sort(key=lambda item: item[0])
    gaps = [
        (nxt - prev).total_seconds() - duration
        for (prev, duration), (nxt, _) in zip(timed, timed[1:], strict=False)
    ]
    return max(gaps, key=abs) if gaps else None


def _fingerprint_payload(
    clips: list[SessionRecord],
    *,
    fill_identities: list[dict[str, Any]],
    venue: str,
    include_manual: bool,
    reconcile_dir: str,
    provider: str,
    flags: list[str],
    schema_version: str,
    app_version: str,
) -> dict[str, Any]:
    return {
        "clips": _clip_keys(clips),
        "fill_identities": fill_identities,
        "venue": venue,
        "include_manual": include_manual,
        "reconcile_dir": reconcile_dir,
        "provider": provider,
        "flags": list(flags),
        "schema_version": schema_version,
        "app_version": app_version,
    }


def _clip_keys(clips: list[SessionRecord]) -> list[dict[str, Any]]:
    return [
        {
            "session_id": record.id,
            "recording_sha256": record.recording.sha256,
            "duration_s": float(record.recording.duration_s),
            "part_shas": [part.sha256 for part in record.recording.parts],
        }
        for record in clips
    ]


def _fill_identities_from_stored(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _canonical_sha256(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _aware_start(record: SessionRecord) -> datetime:
    start = datetime.fromisoformat(record.recording.start_wallclock_vienna)
    if start.tzinfo is None:
        raise DayManifestError(
            f"session {record.id} start_wallclock_vienna must be timezone-aware"
        )
    return start


def csv_calendar_date_for_fill(fill: FillRecord) -> date:
    """CSV-written calendar date. No UTC/Vienna fallback (plan §2.1 / §3.4)."""
    if fill.csv_calendar_date is None:
        raise DayManifestError(
            f"csv_calendar_date fehlt für Fill {fill.fill_id!r}"
        )
    return fill.csv_calendar_date


def fill_in_csv_date_window(fill: FillRecord, day: date) -> bool:
    """True when the CSV's own calendar date is in [D−1, D+1]."""
    civil = csv_calendar_date_for_fill(fill)
    return civil in {day - timedelta(days=1), day, day + timedelta(days=1)}


def csv_window_fill_identities(fills: list[FillRecord], day: date) -> list[FillIdentity]:
    """Identity keys of fills whose CSV-written date is in [D−1, D+1]."""
    identities = [fill_identity(fill) for fill in fills if fill_in_csv_date_window(fill, day)]
    identities.sort(key=_identity_sort)
    return identities


def acquire_day_publish_lock(root: Path, session_id: str) -> Path | None:
    """Lock a built day-path day for ``tva publish``. Does not create ``days/<date>/``."""
    if not day_manifest_enabled():
        return None
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    if not store.session_json_path(root, session_id).is_file():
        return None
    record = store.load_session(root, session_id)
    day = nominal_vienna_date(record)
    folder = day_dir(root, day)
    if not folder.is_dir():
        return None
    if not is_day_path(root, clips_for_day(root, day)):
        return None
    lock = day_lock_path(root, day)
    if not try_acquire_lock(lock, create_parent=False):
        raise DayManifestError(LOCK_ERROR)
    return lock


def _identity_sort(item: FillIdentity) -> tuple[Any, ...]:
    return (
        item.timestamp.isoformat(),
        item.side,
        item.price,
        item.qty if item.qty is not None else -1,
        item.instrument,
        item.contract_month or "",
        item.contract_year if item.contract_year is not None else -1,
        item.source_group_id or "",
    )


def _read_current_build_id(root: Path, day: date) -> str | None:
    path = current_pointer_path(root, day)
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return None
    if not text or "/" in text or "\\" in text or text in {".", ".."}:
        return None
    return text


def _replace_current(root: Path, day: date, build_id: str) -> None:
    dest = current_pointer_path(root, day)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{CURRENT_NAME}.{os.getpid()}.tmp")
    tmp.write_text(build_id + "\n", encoding="utf-8")
    try:
        os.replace(tmp, dest)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _session_days(root: Path) -> list[date]:
    days: set[date] = set()
    for session_id in store.list_session_ids(root):
        record = store.load_session(root, session_id)
        days.add(nominal_vienna_date(record))
    return sorted(days)


def unique_dates(days: list[date]) -> list[date]:
    seen: set[date] = set()
    out: list[date] = []
    for item in days:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _is_iso_date(name: str) -> bool:
    try:
        date.fromisoformat(name)
    except ValueError:
        return False
    return True


def _rmtree(path: Path) -> None:
    import shutil

    shutil.rmtree(path)
