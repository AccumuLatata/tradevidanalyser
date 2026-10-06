"""Exclusive fill ownership (PR-32, plan §2.3). Gate: CD11 / tz_assumption waived."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.day_audit import FillIdentity, audit_path, fill_identity
from tradevidanalyser.day_manifest import (
    DayManifestError,
    LOCK_ERROR,
    TZ_ASSUMPTION_CSV,
    clips_for_day,
    day_lock_path,
    is_day_path,
    load_current_day_json,
    nominal_end_utc,
    nominal_start_utc,
    nominal_vienna_date,
)
from tradevidanalyser.fills import (
    FillsError,
    FillsResult,
    assign_tva_trade_ids,
    fills_table,
    load_fills,
    pair_fills,
    trades_table,
    venue_from_hint,
    write_table,
)
from tradevidanalyser.fills_mirror import FillRecord, JournalTrade
from tradevidanalyser.flags import exclusive_fills_enabled, pause_guard_enabled
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.pause_guard import key_matches, load_pause_check
from tradevidanalyser.schema import IntentProposal, IntentProposals, SessionRecord, alignment_is_invalid
from tradevidanalyser.watch import release_lock, try_acquire_lock

NEAR_BOUNDARY_EPS_S = 10.0
FILLS_MISMATCH_ERROR = (
    "Identitätsschlüssel oder Optionen passen nicht zum letzten Bau, tva day build ausführen"
)
PROPOSAL_DISCARD = "Proposal verworfen, Fill-Schlüssel passt nicht"
TZ_ASSUMPTION_WAIVED = "waived"

OutsideReason = Literal["no_core", "before_first", "after_last", "gap"]


@dataclass
class Core:
    record: SessionRecord
    start: datetime
    end: datetime

    @property
    def session_id(self) -> str:
        return self.record.id


@dataclass
class OutsideFill:
    fill: FillRecord
    reason: OutsideReason
    near_boundary: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = fill_identity(self.fill).as_dict()
        payload["reason"] = self.reason
        if self.near_boundary:
            payload["near_boundary"] = list(self.near_boundary)
        return payload


@dataclass
class ExclusiveBuildResult:
    cascade: dict[str, str]
    remaps: list[dict[str, Any]]
    discarded: list[dict[str, Any]]


@dataclass
class DayOwnership:
    assigned: dict[str, list[FillRecord]]
    outside: list[OutsideFill]
    overlap: dict[str, list[str]]
    near_boundary: dict[str, list[dict[str, Any]]]
    claimed_legacy: list[FillIdentity]
    fill_owner: dict[str, str | None]
    trades: list[JournalTrade]
    tva_trade_ids: list[str]
    session_trades: dict[str, list[tuple[JournalTrade, str]]]
    signatures: dict[str, dict[str, Any]]

    def as_store(self) -> dict[str, Any]:
        return {
            session_id: {
                "fill_identities": [
                    fill_identity(fill).as_dict() for fill in self.assigned.get(session_id, [])
                ],
                "trade_ids": [
                    {"tva_trade_id": tva_id, "entry": _entry_identity(trade).as_dict()}
                    for trade, tva_id in self.session_trades.get(session_id, [])
                ],
            }
            for session_id in self.assigned
        }


def vienna_fill_date(ts: datetime) -> date:
    """Vienna calendar day of a timestamp. Used only while exclusive fills is on (§2.3)."""
    if ts.tzinfo is None:
        raise DayManifestError("fill timestamp must be timezone-aware")
    return ts.astimezone(VIENNA).date()


def core_interval(record: SessionRecord, root: Path) -> tuple[datetime, datetime] | None:
    if not clip_has_core(record, root):
        return None
    start = nominal_start_utc(record)
    end = nominal_end_utc(record)
    return start, end


def clip_has_core(record: SessionRecord, root: Path) -> bool:
    check = load_pause_check(root, record.id) if pause_guard_enabled() else None
    if check is not None and key_matches(check, record) and check.override == "force":
        return True
    if check is not None and key_matches(check, record) and check.pause_check == "suspected":
        return False
    if alignment_is_invalid(record.alignment):
        return False
    if pause_guard_enabled() and (check is None or not key_matches(check, record)):
        return False
    return True


def neighbor_sessions(root: Path, day: date) -> list[SessionRecord]:
    found: list[SessionRecord] = []
    for session_id in store.list_session_ids(root):
        record = store.load_session(root, session_id)
        start_day = nominal_vienna_date(record)
        if start_day not in {day - timedelta(days=1), day + timedelta(days=1)}:
            continue
        if _interval_intersects_day(record, day):
            found.append(record)
    found.sort(key=lambda rec: (nominal_start_utc(rec), rec.id))
    return found


def tz_assumption_for_build(root: Path) -> str:
    path = audit_path(root)
    if not path.is_file():
        return TZ_ASSUMPTION_CSV
    try:
        payload = store.read_json(path)
    except (OSError, ValueError):
        return TZ_ASSUMPTION_CSV
    if isinstance(payload, dict) and payload.get("tz_assumption") == TZ_ASSUMPTION_WAIVED:
        return TZ_ASSUMPTION_WAIVED
    return TZ_ASSUMPTION_CSV


def assign_day_ownership(
    root: Path,
    day: date,
    fills: list[FillRecord],
    *,
    include_manual: bool,
    loader: str,
) -> DayOwnership:
    clips = clips_for_day(root, day)
    neighbors = neighbor_sessions(root, day)
    cores_d = _cores_of(root, clips)
    cores_all = cores_d + _cores_of(root, neighbors)
    assigned: dict[str, list[FillRecord]] = {record.id: [] for record in clips}
    outside: list[OutsideFill] = []
    overlap = _geometric_overlap(cores_all, clip_ids={record.id for record in clips})
    near_boundary: dict[str, list[dict[str, Any]]] = {record.id: [] for record in clips}
    claimed_legacy: list[FillIdentity] = []
    fill_owner: dict[str, str | None] = {}
    legacy_claimed = _legacy_neighbor_identities(root, neighbors)

    for fill in _candidate_fills(fills, day, cores_d):
        ts = _utc(fill.timestamp)
        containing = [core for core in cores_all if core.start <= ts < core.end]
        near = _near_clip_ids(ts, cores_d)
        if not containing:
            if fill_identity(fill) in legacy_claimed:
                claimed_legacy.append(fill_identity(fill))
                continue
            reason = _outside_reason(ts, cores_d)
            outside.append(OutsideFill(fill=fill, reason=reason, near_boundary=near))
            fill_owner[fill.fill_id] = None
            for session_id in near:
                near_boundary[session_id].append(fill_identity(fill).as_dict())
            continue
        chosen = min(containing, key=lambda core: (core.start, core.session_id))
        if nominal_vienna_date(chosen.record) != day:
            continue
        others = [core.session_id for core in containing if core.session_id != chosen.session_id]
        assigned[chosen.session_id].append(fill)
        fill_owner[fill.fill_id] = chosen.session_id
        if others:
            overlap[chosen.session_id] = sorted(set(overlap.get(chosen.session_id, []) + others))
        if near:
            for session_id in near:
                near_boundary[session_id].append(fill_identity(fill).as_dict())

    pairing_fills = [fill for fills_for in assigned.values() for fill in fills_for]
    pairing_fills.extend(item.fill for item in outside)
    pairing_fills.sort(key=lambda fill: (_utc(fill.timestamp), fill.fill_id))
    trades = pair_fills(pairing_fills, include_manual=include_manual, loader=loader)
    tva_ids = assign_tva_trade_ids(trades)
    session_trades: dict[str, list[tuple[JournalTrade, str]]] = {record.id: [] for record in clips}
    for trade, tva_id in zip(trades, tva_ids, strict=True):
        owner = fill_owner.get(trade.entry_fill_id)
        if owner is not None:
            session_trades[owner].append((trade, tva_id))
    signatures = {
        record.id: {
            "fill_identities": [fill_identity(fill).as_dict() for fill in assigned[record.id]],
            "trade_ids": [
                {"tva_trade_id": tva_id, "entry": _entry_identity(trade).as_dict()}
                for trade, tva_id in session_trades[record.id]
            ],
        }
        for record in clips
    }
    return DayOwnership(
        assigned=assigned,
        outside=outside,
        overlap=overlap,
        near_boundary=near_boundary,
        claimed_legacy=claimed_legacy,
        fill_owner=fill_owner,
        trades=trades,
        tva_trade_ids=tva_ids,
        session_trades=session_trades,
        signatures=signatures,
    )


def apply_exclusive_build(
    root: Path,
    day: date,
    ownership: DayOwnership,
    *,
    venue: str,
    previous: dict[str, Any] | None,
    build_dir: Path,
) -> ExclusiveBuildResult:
    """Write parquets, run cascade, rewrite ledger. Returns cascade per session."""
    prev_own = _previous_signatures(previous)
    has_previous_build = previous is not None
    cascade: dict[str, str] = {}
    discarded: list[dict[str, Any]] = []
    remaps: list[dict[str, Any]] = []
    carry: dict[str, list[dict[str, Any]]] = {}
    for session_id, signature in ownership.signatures.items():
        prev_sig = prev_own.get(session_id)
        if prev_sig is None and has_previous_build:
            prev_sig = _signature_from_existing(root, session_id)
        changed = not _signatures_match(prev_sig, signature)
        cascade[session_id] = "changed" if changed else "unchanged"
        if not changed:
            continue
        carry[session_id] = _carry_proposals(root, session_id)
        _backup_session_artifacts(root, session_id, build_dir / "cascade_backup" / session_id)
        _delete_cascade_artifacts(root, session_id)
        store.drop_ledger_session(root, session_id)
    if carry:
        store.write_json(build_dir / "proposals_carry.json", carry)
    _write_session_parquets(root, ownership, venue=venue)
    for session_id, state in cascade.items():
        if state != "changed":
            continue
        from tradevidanalyser.ledger import add_session

        add_session(session_id, root=root)
        mapped, dropped = _remap_proposals(
            root, session_id, ownership, carry.get(session_id) or []
        )
        remaps.extend(mapped)
        discarded.extend(dropped)
    if remaps:
        store.write_json(build_dir / "proposal_remaps.json", remaps)
    if discarded:
        store.write_json(build_dir / "proposals_discarded.json", discarded)
    return ExclusiveBuildResult(cascade=cascade, remaps=remaps, discarded=discarded)


def ingest_fills_exclusive(
    record: SessionRecord,
    executions: Path,
    *,
    root: Path,
    venue: str | None,
    include_manual: bool,
    prefer_import: bool | None,
    reconcile_dir: Path | None,
) -> FillsResult | None:
    """Write the whole day when exclusive+day-path, else None (legacy window)."""
    if not exclusive_fills_enabled():
        return None
    day = nominal_vienna_date(record)
    clips = clips_for_day(root, day)
    if not is_day_path(root, clips):
        return None
    lock = day_lock_path(root, day)
    if not try_acquire_lock(lock):
        raise FillsError(LOCK_ERROR)
    try:
        payload = load_current_day_json(root, day)
        if payload is None:
            raise FillsError("Tag veraltet, tva day build ausführen")
        csv_path = Path(executions)
        chosen_venue = venue_from_hint(csv_path, venue)
        try:
            loader, fills = load_fills(csv_path, prefer_import=prefer_import)
        except Exception as exc:
            raise FillsError(str(exc)) from exc
        if not _options_and_identities_match(
            payload,
            fills,
            day,
            venue=chosen_venue,
            include_manual=include_manual,
            reconcile_dir=str(reconcile_dir) if reconcile_dir is not None else "",
            provider="",
        ):
            raise FillsError(FILLS_MISMATCH_ERROR)
        try:
            ownership = assign_day_ownership(
                root, day, fills, include_manual=include_manual, loader=loader
            )
        except DayManifestError as exc:
            raise FillsError(str(exc)) from exc
        stored = _previous_signatures(payload)
        if stored and stored != ownership.signatures:
            raise FillsError(FILLS_MISMATCH_ERROR)
        _write_session_parquets(root, ownership, venue=chosen_venue)
        for clip in clips:
            store.compute_status(root, clip.id)
        assigned = ownership.assigned.get(record.id, [])
        session_trades = ownership.session_trades.get(record.id, [])
        return FillsResult(
            session_id=record.id,
            loader=loader,
            venue=chosen_venue,
            fills=len(assigned),
            trades=len(session_trades),
            path="fills.parquet" if assigned else "",
            trades_path="trades.parquet" if session_trades else "",
            include_manual=include_manual,
            status="ok",
        )
    finally:
        release_lock(lock)


def _options_and_identities_match(
    payload: dict[str, Any],
    fills: list[FillRecord],
    day: date,
    *,
    venue: str,
    include_manual: bool,
    reconcile_dir: str,
    provider: str,
) -> bool:
    from tradevidanalyser.day_manifest import csv_window_fill_identities

    inputs = payload.get("fingerprint_inputs")
    if not isinstance(inputs, dict):
        return False
    stored_ids = inputs.get("fill_identities")
    if not isinstance(stored_ids, list):
        return False
    got = [item.as_dict() for item in csv_window_fill_identities(fills, day)]
    if got != stored_ids:
        return False
    return (
        str(inputs.get("venue") or "unknown") == venue
        and bool(inputs.get("include_manual")) == include_manual
        and str(inputs.get("reconcile_dir") or "") == reconcile_dir
        and str(inputs.get("provider") or "") == provider
    )


def _candidate_fills(
    fills: list[FillRecord],
    day: date,
    cores_d: list[Core],
) -> list[FillRecord]:
    out: list[FillRecord] = []
    for fill in fills:
        ts = _utc(fill.timestamp)
        in_core = any(core.start <= ts < core.end for core in cores_d)
        if vienna_fill_date(ts) == day or in_core:
            out.append(fill)
    return out


def _geometric_overlap(cores: list[Core], *, clip_ids: set[str]) -> dict[str, list[str]]:
    """Other session ids whose cores overlap a clip of D (plan §2.3 clip field)."""
    overlap: dict[str, list[str]] = {session_id: [] for session_id in clip_ids}
    for index, left in enumerate(cores):
        for right in cores[index + 1 :]:
            if left.start < right.end and right.start < left.end:
                if left.session_id in overlap:
                    overlap[left.session_id].append(right.session_id)
                if right.session_id in overlap:
                    overlap[right.session_id].append(left.session_id)
    return {key: sorted(set(values)) for key, values in overlap.items()}


def _cores_of(root: Path, records: list[SessionRecord]) -> list[Core]:
    cores: list[Core] = []
    for record in records:
        interval = core_interval(record, root)
        if interval is None:
            continue
        start, end = interval
        cores.append(Core(record=record, start=start, end=end))
    cores.sort(key=lambda core: (core.start, core.session_id))
    return cores


def _outside_reason(ts: datetime, cores: list[Core]) -> OutsideReason:
    if not cores:
        return "no_core"
    if ts < cores[0].start:
        return "before_first"
    if ts >= cores[-1].end:
        return "after_last"
    return "gap"


def _near_clip_ids(ts: datetime, cores: list[Core]) -> list[str]:
    near: list[str] = []
    for core in cores:
        if abs((ts - core.start).total_seconds()) <= NEAR_BOUNDARY_EPS_S:
            near.append(core.session_id)
        elif abs((ts - core.end).total_seconds()) <= NEAR_BOUNDARY_EPS_S:
            near.append(core.session_id)
    return near


def _interval_intersects_day(record: SessionRecord, day: date) -> bool:
    start = nominal_start_utc(record).astimezone(VIENNA)
    end = nominal_end_utc(record).astimezone(VIENNA)
    day_start = datetime.combine(day, time.min, tzinfo=VIENNA)
    day_end = day_start + timedelta(days=1)
    return start < day_end and end > day_start


def _legacy_neighbor_identities(root: Path, neighbors: list[SessionRecord]) -> set[FillIdentity]:
    claimed: set[FillIdentity] = set()
    for record in neighbors:
        neighbor_day = nominal_vienna_date(record)
        if is_day_path(root, clips_for_day(root, neighbor_day)):
            continue
        path = store.fills_path(root, record.id)
        if not path.is_file():
            continue
        claimed.update(_identities_from_parquet(path))
    return claimed


def _identities_from_parquet(path: Path) -> list[FillIdentity]:
    from tradevidanalyser.day_audit import DayAuditError, _identities_from_fills_table

    try:
        table = pq.read_table(path)
        return _identities_from_fills_table(table)
    except (DayAuditError, OSError, ValueError) as exc:
        raise DayManifestError(f"Legacy-Nachbar fills.parquet unlesbar: {path}") from exc


def _write_session_parquets(root: Path, ownership: DayOwnership, *, venue: str) -> None:
    for session_id, fills in ownership.assigned.items():
        fills_file = store.fills_path(root, session_id)
        trades_file = store.trades_path(root, session_id)
        if not fills:
            fills_file.unlink(missing_ok=True)
            trades_file.unlink(missing_ok=True)
            store.compute_status(root, session_id)
            continue
        write_table(fills_file, fills_table(fills, venue=venue))
        paired = ownership.session_trades.get(session_id, [])
        if not paired:
            trades_file.unlink(missing_ok=True)
        else:
            trades = [item[0] for item in paired]
            ids = [item[1] for item in paired]
            write_table(trades_file, trades_table(trades, venue=venue, tva_trade_ids=ids))
        store.compute_status(root, session_id)


def _previous_signatures(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    stored = payload.get("ownership")
    if isinstance(stored, dict):
        return stored
    return {}


def _signature_from_existing(root: Path, session_id: str) -> dict[str, Any]:
    fills_file = store.fills_path(root, session_id)
    fill_identities = (
        [item.as_dict() for item in _identities_from_parquet(fills_file)]
        if fills_file.is_file()
        else []
    )
    trade_ids = [
        {"tva_trade_id": tva_id, "entry": identity.as_dict()}
        for tva_id, identity in _trade_entry_identities(store.trades_path(root, session_id)).items()
    ]
    return {"fill_identities": fill_identities, "trade_ids": trade_ids}


def _signatures_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if left is None or right is None:
        return False
    left_fills = {_identity_key(item) for item in left.get("fill_identities") or [] if isinstance(item, dict)}
    right_fills = {_identity_key(item) for item in right.get("fill_identities") or [] if isinstance(item, dict)}
    if left_fills != right_fills:
        return False
    left_trades = {
        (str(item.get("tva_trade_id") or ""), _identity_key(item.get("entry") or {}))
        for item in left.get("trade_ids") or []
        if isinstance(item, dict)
    }
    right_trades = {
        (str(item.get("tva_trade_id") or ""), _identity_key(item.get("entry") or {}))
        for item in right.get("trade_ids") or []
        if isinstance(item, dict)
    }
    return left_trades == right_trades


def _identity_key(payload: object) -> tuple[Any, ...]:
    if not isinstance(payload, dict):
        return ()
    raw = payload.get("timestamp")
    if isinstance(raw, datetime):
        ts = _utc(raw).isoformat()
    else:
        ts = _norm_iso(str(raw or ""))
    price = payload.get("price")
    try:
        price_n = round(float(price or 0.0), 6)
    except (TypeError, ValueError):
        price_n = 0.0
    return (
        ts,
        str(payload.get("side") or ""),
        price_n,
        payload.get("qty"),
        str(payload.get("instrument") or ""),
        payload.get("contract_month"),
        payload.get("contract_year"),
        payload.get("source_group_id"),
    )


def _norm_iso(text: str) -> str:
    if not text:
        return ""
    cleaned = text.replace("Z", "+00:00")
    if len(cleaned) >= 5 and cleaned[-5] in "+-" and cleaned[-3] != ":":
        cleaned = cleaned[:-2] + ":" + cleaned[-2:]
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _backup_session_artifacts(root: Path, session_id: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for path in (
        store.evidence_path(root, session_id),
        store.context_path(root, session_id),
        store.debrief_json_path(root, session_id),
        store.debrief_md_path(root, session_id),
        store.proposals_path(root, session_id),
        store.tradesviz_tags_path(root, session_id),
    ):
        if path.is_file():
            shutil.copy2(path, dest / path.name)


def _delete_cascade_artifacts(root: Path, session_id: str) -> None:
    store.evidence_path(root, session_id).unlink(missing_ok=True)
    store.context_path(root, session_id).unlink(missing_ok=True)
    store.drop_debrief(root, session_id)
    store.drop_proposals(root, session_id)


def _carry_proposals(root: Path, session_id: str) -> list[dict[str, Any]]:
    path = store.proposals_path(root, session_id)
    if not path.is_file():
        return []
    try:
        payload = store.read_json(path)
    except (OSError, ValueError):
        return []
    trades_path = store.trades_path(root, session_id)
    entry_by_id = _trade_entry_identities(trades_path)
    out: list[dict[str, Any]] = []
    for item in payload.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status not in {"confirmed", "rejected"}:
            continue
        tva_id = str(item.get("tva_trade_id") or "")
        identity = entry_by_id.get(tva_id)
        if identity is None:
            continue
        out.append(
            {
                "tva_trade_id": tva_id,
                "status": status,
                "fill_identity": identity.as_dict(),
                "proposed_tags": list(item.get("proposed_tags") or []),
                "source_segs": list(item.get("source_segs") or []),
            }
        )
    return out


def _remap_proposals(
    root: Path,
    session_id: str,
    ownership: DayOwnership,
    carried: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not carried:
        return [], []
    by_entry = {
        _canon(_entry_identity(trade).as_dict()): tva_id
        for trade, tva_id in ownership.session_trades.get(session_id, [])
    }
    kept: list[dict[str, Any]] = []
    remaps: list[dict[str, Any]] = []
    discarded: list[dict[str, Any]] = []
    for item in carried:
        identity = item.get("fill_identity")
        new_id = by_entry.get(_canon(identity if isinstance(identity, dict) else {}))
        old_id = item.get("tva_trade_id")
        if new_id is None:
            discarded.append(
                {
                    "session_id": session_id,
                    "reason": PROPOSAL_DISCARD,
                    "old_tva_trade_id": old_id,
                    "fill_identity": identity,
                }
            )
            continue
        kept.append(
            {
                "tva_trade_id": new_id,
                "status": item.get("status"),
                "proposed_tags": item.get("proposed_tags") or [],
                "source_segs": item.get("source_segs") or [],
                "previous_tva_trade_id": old_id,
            }
        )
        if old_id != new_id:
            remaps.append(
                {
                    "session_id": session_id,
                    "old_tva_trade_id": old_id,
                    "new_tva_trade_id": new_id,
                }
            )
    if kept:
        from tradevidanalyser.proposals import write_tradesviz_csv

        report = IntentProposals(
            schema_version="1",
            session_id=session_id,
            proposals=[
                IntentProposal(
                    tva_trade_id=str(item["tva_trade_id"]),
                    proposed_tags=list(item["proposed_tags"]),
                    source_segs=list(item["source_segs"]),
                    status="confirmed" if item["status"] == "confirmed" else "rejected",
                )
                for item in kept
            ],
            gaps=[],
        )
        store.write_json(store.proposals_path(root, session_id), report.model_dump(mode="json"))
        write_tradesviz_csv(root, session_id, report)
    return remaps, discarded


def _trade_entry_identities(path: Path) -> dict[str, FillIdentity]:
    if not path.is_file():
        return {}
    table = pq.read_table(path)
    if "tva_trade_id" not in table.column_names:
        return {}
    out: dict[str, FillIdentity] = {}
    for row in table.to_pylist():
        tva_id = str(row.get("tva_trade_id") or "")
        if not tva_id:
            continue
        direction = str(row.get("direction") or "long")
        side = "buy" if direction == "long" else "sell"
        ts = row.get("entry_timestamp")
        if ts is None:
            continue
        out[tva_id] = FillIdentity(
            timestamp=_utc(ts),
            side=side,
            price=float(row.get("entry_price") or 0.0),
            qty=int(row["qty"]) if row.get("qty") is not None else None,
            instrument=str(row.get("instrument") or "").strip().upper(),
            contract_month=str(row["contract_month"]) if row.get("contract_month") else None,
            contract_year=int(row["contract_year"]) if row.get("contract_year") is not None else None,
            source_group_id=str(row["source_group_id"]) if row.get("source_group_id") else None,
        )
    return out


def _entry_identity(trade: JournalTrade) -> FillIdentity:
    side = "buy" if trade.direction == "long" else "sell"
    return FillIdentity(
        timestamp=_utc(trade.entry_timestamp),
        side=side,
        price=float(trade.entry_price),
        qty=int(trade.qty),
        instrument=str(trade.instrument).strip().upper(),
        contract_month=trade.contract_month,
        contract_year=trade.contract_year,
        source_group_id=trade.source_group_id,
    )


def _canon(payload: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(payload.get("timestamp") or ""),
        str(payload.get("side") or ""),
        float(payload.get("price") or 0.0),
        payload.get("qty"),
        str(payload.get("instrument") or ""),
        payload.get("contract_month"),
        payload.get("contract_year"),
        payload.get("source_group_id"),
    )


def _utc(value: datetime) -> datetime:
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if not isinstance(value, datetime):
        raise DayManifestError(f"timestamp is not datetime: {value!r}")
    if value.tzinfo is None:
        raise DayManifestError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


