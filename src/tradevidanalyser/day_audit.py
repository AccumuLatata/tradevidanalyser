"""Read-only fills audit → ``TVA_ROOT/days/audit.json`` (PR-28, plan §2.7).

Does not recompute session windows to explain 20/88. Does not write session
files, parquets, OCR, Notion, or ``pause_checks/``. No day-path flags.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.fills import load_fills, session_utc_window
from tradevidanalyser.fills_mirror import FillRecord
from tradevidanalyser.ingest import SPLIT_GAP_S
from tradevidanalyser.naming import FilenameError, VIENNA, obs_name_prefix, parse_obs_filename, vienna_today
from tradevidanalyser.schema import RecordingPart, SessionRecord

AUDIT_SCHEMA_VERSION = "1"
AUDIT_RELATIVE_PATH = "days/audit.json"
DEFAULT_AUDIT_FROM = date(2026, 9, 14)
TZ_ASSUMPTION_CSV = "csv_offset_as_is"
TZ_ASSUMPTION_WAIVED = "waived"
FILLS_MATCH_OK = "ok"
FILLS_MATCH_NOT_RUN = "not_run"
OTHER_ACCOUNT = "not_in_schema"
KNOWN_INSTRUMENTS = frozenset({"MNQ", "MES"})
SHIFT_HOURS = (-6, -5, -4, -2, -1, 1, 2)
TRUE_TIME_RULE = "csv_timestamp + delta_hours"
MTIME_HINT_NOTE = "unverified"


class DayAuditError(ValueError):
    pass


@dataclass(frozen=True)
class FillIdentity:
    timestamp: datetime
    side: str
    price: float
    qty: int | None
    instrument: str
    contract_month: str | None
    contract_year: int | None
    source_group_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "side": self.side,
            "price": self.price,
            "qty": self.qty,
            "instrument": self.instrument,
            "contract_month": self.contract_month,
            "contract_year": self.contract_year,
            "source_group_id": self.source_group_id,
        }


@dataclass
class DayAuditResult:
    date_from: date
    date_to: date
    path: str
    fills_match: str
    tz_assumption: str = TZ_ASSUMPTION_CSV
    loader: str = "unknown"
    clock_note: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        body = dict(self.payload)
        body.update(
            {
                "status": "ok",
                "path": self.path,
                "from": self.date_from.isoformat(),
                "to": self.date_to.isoformat(),
                "fills_match": self.fills_match,
                "tz_assumption": self.tz_assumption,
                "loader": self.loader,
            }
        )
        return body


def audit_path(root: Path) -> Path:
    return Path(root) / AUDIT_RELATIVE_PATH


def parse_audit_date(raw: str | date | None, *, default: date | None = None) -> date:
    if raw is None:
        if default is None:
            raise DayAuditError("date is required")
        return default
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw
    text = str(raw).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise DayAuditError(f"date must be YYYY-MM-DD (got {raw!r})") from exc


def fill_identity(fill: FillRecord) -> FillIdentity:
    return _identity(
        timestamp=fill.timestamp,
        side=fill.side,
        price=fill.price,
        qty=fill.qty,
        instrument=fill.instrument,
        contract_month=fill.contract_month,
        contract_year=fill.contract_year,
        source_group_id=fill.source_group_id,
    )


def run_day_audit(
    *,
    root: Path,
    date_from: str | date | None = None,
    date_to: str | date | None = None,
    executions: Path | None = None,
    loader: str = "unknown",
    clock_note: str | None = None,
    sample_clocks: bool = False,
) -> DayAuditResult:
    start = parse_audit_date(date_from, default=DEFAULT_AUDIT_FROM)
    end = parse_audit_date(date_to, default=vienna_today())
    if end < start:
        raise DayAuditError(f"--from {start.isoformat()} is after --to {end.isoformat()}")
    reported_loader = (loader or "unknown").strip().lower() or "unknown"
    if reported_loader not in {"unknown", "mirror", "import"}:
        raise DayAuditError("loader must be unknown, mirror, or import")
    prefer_import = {"mirror": False, "import": True}.get(reported_loader)

    dest = audit_path(root)
    existing = _existing_audit(dest)
    _refuse_if_waived(existing)
    clock_note = _preserved_clock_note(existing, clock_note)
    visible_fills = _preserved_visible_fills(existing)

    sessions = _sessions_in_range(root, start, end)
    csv_fills: list[FillRecord] | None = None
    fills_match = FILLS_MATCH_NOT_RUN
    csv_path: str | None = None
    if executions is not None:
        path = Path(executions)
        if not path.is_file():
            raise DayAuditError(f"executions file not found: {path}")
        try:
            _used_loader, csv_fills = load_fills(path, prefer_import=prefer_import)
        except Exception as exc:
            raise DayAuditError(str(exc)) from exc
        fills_match = FILLS_MATCH_OK
        csv_path = str(path)
        del _used_loader

    payload = _build_payload(
        root,
        sessions=sessions,
        csv_fills=csv_fills,
        fills_match=fills_match,
        reported_loader=reported_loader,
        clock_note=clock_note,
        executions_path=csv_path,
        date_from=start,
        date_to=end,
        visible_fills=visible_fills,
        sample_clocks=sample_clocks,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(dest, payload)
    return DayAuditResult(
        date_from=start,
        date_to=end,
        path=AUDIT_RELATIVE_PATH,
        fills_match=fills_match,
        tz_assumption=TZ_ASSUMPTION_CSV,
        loader=reported_loader,
        clock_note=clock_note,
        payload=payload,
    )


def _build_payload(
    root: Path,
    *,
    sessions: list[SessionRecord],
    csv_fills: list[FillRecord] | None,
    fills_match: str,
    reported_loader: str,
    clock_note: str | None,
    executions_path: str | None,
    date_from: date,
    date_to: date,
    visible_fills: list[Any] | None = None,
    sample_clocks: bool = False,
) -> dict[str, Any]:
    parquet_by_session = _read_session_parquets(root, sessions)
    all_fill_ids: set[FillIdentity] = set()
    all_trade_ids: set[FillIdentity] = set()
    parquet_other: list[FillIdentity] = []
    for pack in parquet_by_session.values():
        all_fill_ids.update(pack["fill_identities"])
        all_trade_ids.update(pack["trade_identities"])
        parquet_other.extend(pack["other_instrument"])

    missing_fills_sessions = [
        record.id for record in sessions if not store.fills_path(root, record.id).is_file()
    ]
    windows = {record.id: session_utc_window(record) for record in sessions}

    classes: dict[str, Any]
    window_block: dict[str, Any] | None
    shifts_block: dict[str, Any] | None
    csv_identities: list[FillIdentity] = []
    if csv_fills is None:
        classes = {
            "in_existing_parquet": 0,
            "session_without_fills_run": len(missing_fills_sessions),
            "filtered_manual": 0,
            "other_instrument": len(parquet_other),
            "other_account": OTHER_ACCOUNT,
        }
        window_block = None
        shifts_block = None
    else:
        csv_identities = [fill_identity(fill) for fill in csv_fills]
        csv_id_set = set(csv_identities)
        other_instrument_n = sum(
            1 for ident in csv_identities if ident.instrument not in KNOWN_INSTRUMENTS
        ) + sum(1 for ident in parquet_other if ident not in csv_id_set)
        in_parquet = [ident for ident in csv_identities if ident in all_fill_ids]
        filtered_manual = [
            fill
            for fill in csv_fills
            if fill_identity(fill) in all_fill_ids
            and fill_identity(fill) not in all_trade_ids
            and fill.entry_kind == "manual"
        ]
        classes = {
            "in_existing_parquet": len(in_parquet),
            "session_without_fills_run": len(missing_fills_sessions),
            "filtered_manual": len(filtered_manual),
            "other_instrument": other_instrument_n,
            "other_account": OTHER_ACCOUNT,
        }
        window_counts = _window_counts(csv_fills, windows)
        outside_per_session = {
            record.id: _fills_outside_window(csv_fills, windows[record.id]) for record in sessions
        }
        window_block = {
            "tz_assumption": TZ_ASSUMPTION_CSV,
            "in_one": window_counts["in_one"],
            "in_many": window_counts["in_many"],
            "outside_all": window_counts["outside_all"],
            "fills_outside_window": outside_per_session,
        }
        shifts_block = {
            "true_time": TRUE_TIME_RULE,
            "tz_assumption": TZ_ASSUMPTION_CSV,
            "by_delta_hours": {
                _shift_key(delta): _shift_counts(csv_fills, windows, delta) for delta in SHIFT_HOURS
            },
        }

    session_rows = [
        _session_row(
            root,
            record,
            pack=parquet_by_session.get(record.id),
            csv_fills=csv_fills,
            window=windows[record.id],
            missing_fills=record.id in missing_fills_sessions,
        )
        for record in sessions
    ]
    payload = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "from": date_from.isoformat(),
        "to": date_to.isoformat(),
        "tz_assumption": TZ_ASSUMPTION_CSV,
        "loader": reported_loader,
        "fills_match": fills_match,
        "executions": executions_path,
        "clock_note": clock_note,
        "visible_fills": list(visible_fills or []),
        "sessions": session_rows,
        "sessions_without_fills_run": missing_fills_sessions,
        "unstitched_gaps": _unstitched_gaps(sessions),
        "classes": classes,
        "window": window_block,
        "shifts": shifts_block,
        "mtime_pause_hint": MTIME_HINT_NOTE,
    }
    if sample_clocks:
        from tradevidanalyser.pause_guard import sample_clocks_readonly

        payload["clock_samples"] = [
            sample_clocks_readonly(record, root=root) for record in sessions
        ]
    return payload


def _session_row(
    root: Path,
    record: SessionRecord,
    *,
    pack: dict[str, Any] | None,
    csv_fills: list[FillRecord] | None,
    window: tuple[datetime, datetime],
    missing_fills: bool,
) -> dict[str, Any]:
    venue = None if pack is None else pack.get("venue")
    fills_n = 0 if pack is None else pack["fills_n"]
    trades_n = 0 if pack is None else pack["trades_n"]
    outside = None if csv_fills is None else _fills_outside_window(csv_fills, window)
    return {
        "session_id": record.id,
        "nominal_start": record.recording.start_wallclock_vienna,
        "duration_s": float(record.recording.duration_s),
        "has_fills_parquet": not missing_fills,
        "has_trades_parquet": store.trades_path(root, record.id).is_file(),
        "venue": venue,
        "fills_in_parquet": fills_n,
        "trades_in_parquet": trades_n,
        "fills_outside_window": outside,
        "mtime_pause_hint_s": _mtime_pause_hint_s(root, record),
        "mtime_pause_hint": MTIME_HINT_NOTE,
        "stitched_gap_s": _stitched_gap_s(record),
        "class": "session_without_fills_run" if missing_fills else None,
    }


def _sessions_in_range(root: Path, start: date, end: date) -> list[SessionRecord]:
    found: list[SessionRecord] = []
    for session_id in store.list_session_ids(root):
        record = store.load_session(root, session_id)
        day = _nominal_vienna_date(record)
        if start <= day <= end:
            found.append(record)
    found.sort(key=lambda rec: (_nominal_start_utc(rec), rec.id))
    return found


def _nominal_vienna_date(record: SessionRecord) -> date:
    start = datetime.fromisoformat(record.recording.start_wallclock_vienna)
    if start.tzinfo is None:
        raise DayAuditError(
            f"session {record.id} start_wallclock_vienna must be timezone-aware"
        )
    return start.astimezone(VIENNA).date()


def _nominal_start_utc(record: SessionRecord) -> datetime:
    start = datetime.fromisoformat(record.recording.start_wallclock_vienna)
    if start.tzinfo is None:
        raise DayAuditError(
            f"session {record.id} start_wallclock_vienna must be timezone-aware"
        )
    return start.astimezone(timezone.utc)


def _read_session_parquets(root: Path, sessions: list[SessionRecord]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for record in sessions:
        fills_file = store.fills_path(root, record.id)
        trades_file = store.trades_path(root, record.id)
        if not fills_file.is_file() and not trades_file.is_file():
            continue
        fill_ids: set[FillIdentity] = set()
        other: list[FillIdentity] = []
        venue: str | list[str] | None = None
        fills_n = 0
        if fills_file.is_file():
            table = pq.read_table(fills_file)
            fill_list = _identities_from_fills_table(table)
            fill_ids = set(fill_list)
            other = [ident for ident in fill_list if ident.instrument not in KNOWN_INSTRUMENTS]
            venue = _venues_from_table(table)
            fills_n = table.num_rows
        trade_ids: set[FillIdentity] = set()
        trades_n = 0
        if trades_file.is_file():
            trades = pq.read_table(trades_file)
            trade_ids = _identities_from_trades_table(trades)
            trades_n = trades.num_rows
        out[record.id] = {
            "fill_identities": fill_ids,
            "trade_identities": trade_ids,
            "other_instrument": other,
            "venue": venue,
            "fills_n": fills_n,
            "trades_n": trades_n,
        }
    return out


def _venues_from_table(table: Any) -> str | list[str] | None:
    if "venue" not in table.column_names:
        return None
    values = [str(v) for v in table.column("venue").to_pylist() if v not in (None, "")]
    unique = sorted(set(values))
    if not unique:
        return None
    if len(unique) == 1:
        return unique[0]
    return unique


def _identities_from_fills_table(table: Any) -> list[FillIdentity]:
    needed = (
        "timestamp",
        "side",
        "price",
        "qty",
        "instrument",
        "contract_month",
        "contract_year",
        "source_group_id",
    )
    missing = [name for name in needed if name not in table.column_names]
    if missing:
        raise DayAuditError(
            "fills.parquet missing identity columns: " + ", ".join(missing)
        )
    rows = table.select(list(needed)).to_pydict()
    n = len(rows["timestamp"])
    out: list[FillIdentity] = []
    for i in range(n):
        out.append(
            _identity(
                timestamp=rows["timestamp"][i],
                side=rows["side"][i],
                price=rows["price"][i],
                qty=rows["qty"][i],
                instrument=rows["instrument"][i],
                contract_month=rows["contract_month"][i],
                contract_year=rows["contract_year"][i],
                source_group_id=rows["source_group_id"][i],
            )
        )
    return out


def _identities_from_trades_table(table: Any) -> set[FillIdentity]:
    needed = (
        "entry_timestamp",
        "exit_timestamp",
        "entry_price",
        "exit_price",
        "qty",
        "instrument",
        "contract_month",
        "contract_year",
        "source_group_id",
        "direction",
    )
    missing = [name for name in needed if name not in table.column_names]
    if missing:
        raise DayAuditError(
            "trades.parquet missing identity columns: " + ", ".join(missing)
        )
    rows = table.select(list(needed)).to_pydict()
    n = len(rows["entry_timestamp"])
    out: set[FillIdentity] = set()
    for i in range(n):
        direction = str(rows["direction"][i] or "")
        entry_side = "buy" if direction == "long" else "sell"
        exit_side = "sell" if direction == "long" else "buy"
        out.add(
            _identity(
                timestamp=rows["entry_timestamp"][i],
                side=entry_side,
                price=rows["entry_price"][i],
                qty=rows["qty"][i],
                instrument=rows["instrument"][i],
                contract_month=rows["contract_month"][i],
                contract_year=rows["contract_year"][i],
                source_group_id=rows["source_group_id"][i],
            )
        )
        if rows["exit_timestamp"][i] is not None and rows["exit_price"][i] is not None:
            out.add(
                _identity(
                    timestamp=rows["exit_timestamp"][i],
                    side=exit_side,
                    price=rows["exit_price"][i],
                    qty=rows["qty"][i],
                    instrument=rows["instrument"][i],
                    contract_month=rows["contract_month"][i],
                    contract_year=rows["contract_year"][i],
                    source_group_id=rows["source_group_id"][i],
                )
            )
    return out


def _identity(
    *,
    timestamp: Any,
    side: Any,
    price: Any,
    qty: Any,
    instrument: Any,
    contract_month: Any,
    contract_year: Any,
    source_group_id: Any,
) -> FillIdentity:
    return FillIdentity(
        timestamp=_as_utc(timestamp),
        side=str(side).strip().lower(),
        price=float(price),
        qty=_opt_int(qty),
        instrument=str(instrument).strip().upper(),
        contract_month=_opt_str(contract_month),
        contract_year=_opt_int(contract_year),
        source_group_id=_opt_str(source_group_id),
    )


def _as_utc(value: Any) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise DayAuditError(f"timestamp must be datetime (got {value!r})")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, float) and value != value:  # NaN
        return None
    return int(value)


def _windows_containing(
    timestamp: datetime,
    windows: dict[str, tuple[datetime, datetime]],
    sessions: list[SessionRecord],
) -> list[str]:
    instant = _as_utc(timestamp)
    hits: list[str] = []
    for record in sessions:
        start, end = windows[record.id]
        if start <= instant <= end:
            hits.append(record.id)
    return hits


def _window_counts(
    fills: list[FillRecord], windows: dict[str, tuple[datetime, datetime]]
) -> dict[str, int]:
    in_one = 0
    in_many = 0
    outside_all = 0
    for fill in fills:
        n = _window_hit_count(fill.timestamp, windows)
        if n == 0:
            outside_all += 1
        elif n == 1:
            in_one += 1
        else:
            in_many += 1
    return {"in_one": in_one, "in_many": in_many, "outside_all": outside_all}


def _window_hit_count(timestamp: datetime, windows: dict[str, tuple[datetime, datetime]]) -> int:
    instant = _as_utc(timestamp)
    return sum(1 for start, end in windows.values() if start <= instant <= end)


def _fills_outside_window(
    fills: list[FillRecord], window: tuple[datetime, datetime]
) -> int:
    start, end = window
    n = 0
    for fill in fills:
        instant = _as_utc(fill.timestamp)
        if instant < start or instant > end:
            n += 1
    return n


def _shift_key(delta_hours: int) -> str:
    return f"{delta_hours:+d}"


def _shift_counts(
    fills: list[FillRecord],
    windows: dict[str, tuple[datetime, datetime]],
    delta_hours: int,
) -> dict[str, int]:
    delta = timedelta(hours=delta_hours)
    in_one = 0
    in_many = 0
    outside_all = 0
    for fill in fills:
        shifted = _as_utc(fill.timestamp) + delta
        n = _window_hit_count(shifted, windows)
        if n == 0:
            outside_all += 1
        elif n == 1:
            in_one += 1
        else:
            in_many += 1
    return {
        "delta_hours": delta_hours,
        "in_one": in_one,
        "in_many": in_many,
        "outside_all": outside_all,
    }


def _mtime_pause_hint_s(root: Path, record: SessionRecord) -> float | None:
    path = _latest_recording_path(root, record)
    if path is None or not path.is_file():
        return None
    start = _nominal_start_utc(record)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (mtime - start).total_seconds() - float(record.recording.duration_s)


def _latest_recording_path(root: Path, record: SessionRecord) -> Path | None:
    """mtime of the last part in chain order (stop time), not the newest inode."""
    ordered: list[Path] = []
    if record.recording.parts:
        timed = []
        for part in record.recording.parts:
            rel = part.path or ""
            if not rel:
                continue
            name = part.filename or Path(rel).name
            try:
                _sid, start = parse_obs_filename(name)
            except FilenameError:
                continue
            timed.append((start, Path(root) / rel))
        timed.sort(key=lambda item: item[0])
        ordered = [path for _start, path in timed]
    if record.recording.path:
        fallback = Path(root) / record.recording.path
        if fallback not in ordered:
            ordered.append(fallback)
    existing = [path for path in ordered if path.is_file()]
    if not existing:
        return None
    return existing[-1]


def _stitched_gap_s(record: SessionRecord) -> float | None:
    parts = list(record.recording.parts or [])
    if len(parts) < 2:
        return None
    timed = _part_starts(parts)
    if len(timed) < 2:
        return None
    timed.sort(key=lambda item: item[0])
    gaps = [
        (nxt - prev).total_seconds() - duration
        for (prev, duration), (nxt, _) in zip(timed, timed[1:], strict=False)
    ]
    return max(gaps, key=abs) if gaps else None


def _part_starts(parts: list[RecordingPart]) -> list[tuple[datetime, float]]:
    timed: list[tuple[datetime, float]] = []
    for part in parts:
        name = part.filename or Path(part.path).name
        try:
            _sid, start = parse_obs_filename(name)
        except FilenameError:
            continue
        timed.append((start, float(part.duration_s)))
    return timed


def _unstitched_gaps(sessions: list[SessionRecord]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    by_prefix: dict[str, list[SessionRecord]] = {}
    for record in sessions:
        prefix = obs_name_prefix(record.recording.filename or record.recording.path or "")
        if prefix is None:
            continue
        by_prefix.setdefault(prefix, []).append(record)
    for prefix, group in by_prefix.items():
        ordered = sorted(group, key=lambda rec: (_nominal_start_utc(rec), rec.id))
        for prev, nxt in zip(ordered, ordered[1:], strict=False):
            gap = (_nominal_start_utc(nxt) - _nominal_start_utc(prev)).total_seconds() - float(
                prev.recording.duration_s
            )
            if abs(gap) <= SPLIT_GAP_S:
                continue
            rows.append(
                {
                    "prefix": prefix,
                    "left_session_id": prev.id,
                    "right_session_id": nxt.id,
                    "left_filename": prev.recording.filename,
                    "right_filename": nxt.recording.filename,
                    "gap_s": gap,
                }
            )
    rows.sort(key=lambda row: (row["left_session_id"], row["right_session_id"]))
    return rows


def _existing_audit(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        payload = store.read_json(path)
    except (OSError, ValueError) as exc:
        raise DayAuditError(f"cannot read existing {AUDIT_RELATIVE_PATH}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DayAuditError(f"existing {AUDIT_RELATIVE_PATH} is not a JSON object")
    return payload


def _refuse_if_waived(existing: dict[str, Any] | None) -> None:
    if existing is None:
        return
    if existing.get("tz_assumption") == TZ_ASSUMPTION_WAIVED:
        raise DayAuditError(
            f"{AUDIT_RELATIVE_PATH} has tz_assumption={TZ_ASSUMPTION_WAIVED}; "
            "refusing to overwrite the CD11 waiver"
        )


def _preserved_visible_fills(existing: dict[str, Any] | None) -> list[Any]:
    if existing is None:
        return []
    raw = existing.get("visible_fills")
    return list(raw) if isinstance(raw, list) else []


def _preserved_clock_note(
    existing: dict[str, Any] | None, clock_note: str | None
) -> str | None:
    if clock_note is not None:
        return clock_note
    if existing is None:
        return None
    note = existing.get("clock_note")
    return note if isinstance(note, str) and note else None
