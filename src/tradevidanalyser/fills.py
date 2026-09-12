"""TradesViz executions → ``fills.parquet`` / ``trades.parquet`` (TVA4).

Import ThesisTester when ``tradevidanalyser[journal]`` is installed; otherwise
use ``fills_mirror``. Window, ``venue``, and ``tva_trade_id`` are TVA-owned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.fills_mirror import (
    FILL_RECORD_COLUMNS,
    JOURNAL_TRADE_COLUMNS,
    TRADESVIZ_EXECUTIONS_PROFILE,
    FillRecord,
    JournalIngestError,
    JournalTrade,
    fill_to_dict,
    load_tradesviz_executions as mirror_load,
    pair_journal_trades as mirror_pair,
    trade_to_dict,
)
from tradevidanalyser.schema import SessionRecord

VENUES = ("topstepx", "amp", "unknown")
FORBIDDEN_COST_COLUMNS = ("commission", "fees")
WINDOW_PAD = timedelta(minutes=30)
TVA_FILL_COLUMNS = FILL_RECORD_COLUMNS + ("venue",)
TVA_TRADE_COLUMNS = JOURNAL_TRADE_COLUMNS + ("venue", "tva_trade_id")
_VENUE_TOKEN_RE = re.compile(r"[^a-z0-9]+")


class FillsError(RuntimeError):
    pass


@dataclass(frozen=True)
class FillsResult:
    session_id: str
    loader: str
    venue: str
    fills: int
    trades: int
    path: str
    trades_path: str
    include_manual: bool
    status: str = "ok"
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "status": self.status,
            "session_id": self.session_id,
            "loader": self.loader,
            "venue": self.venue,
            "fills": self.fills,
            "trades": self.trades,
            "path": self.path,
            "trades_path": self.trades_path,
            "include_manual": self.include_manual,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def thesistester_available() -> bool:
    try:
        from thesistester.journal.pair import pair_journal_trades  # noqa: F401
        from thesistester.journal.tradesviz import load_tradesviz_executions  # noqa: F401
    except ImportError:
        return False
    return True


def venue_from_hint(path: Path, explicit: str | None) -> str:
    if explicit is not None and str(explicit).strip():
        venue = str(explicit).strip().lower()
        if venue not in VENUES:
            raise FillsError(f"venue must be topstepx, amp, or unknown (got {explicit!r})")
        return venue
    tokens = {part for part in _VENUE_TOKEN_RE.split(path.name.lower()) if part}
    if "topstepx" in tokens or "topstep" in tokens:
        return "topstepx"
    if "amp" in tokens:
        return "amp"
    return "unknown"


def _is_journal_ingest_error(exc: BaseException) -> bool:
    """True for mirror *or* ThesisTester ``JournalIngestError`` (distinct classes)."""
    if isinstance(exc, JournalIngestError):
        return True
    return type(exc).__name__ == "JournalIngestError" and isinstance(exc, Exception)


def session_utc_window(record: SessionRecord) -> tuple[datetime, datetime]:
    start = datetime.fromisoformat(record.recording.start_wallclock_vienna)
    if start.tzinfo is None:
        raise FillsError("session start_wallclock_vienna must be timezone-aware")
    start_utc = start.astimezone(timezone.utc)
    end_utc = start_utc + timedelta(seconds=float(record.recording.duration_s))
    return start_utc - WINDOW_PAD, end_utc + WINDOW_PAD


def filter_fills_to_window(
    fills: list[FillRecord], start_utc: datetime, end_utc: datetime
) -> list[FillRecord]:
    kept: list[FillRecord] = []
    for fill in fills:
        ts = fill.timestamp
        if ts.tzinfo is None:
            raise FillsError(f"fill {fill.fill_id!r} timestamp must be tz-aware")
        instant = ts.astimezone(timezone.utc)
        if start_utc <= instant <= end_utc:
            kept.append(fill)
    return kept


def assign_tva_trade_ids(trades: list[JournalTrade]) -> list[str]:
    ordered = sorted(range(len(trades)), key=lambda i: (trades[i].entry_timestamp, trades[i].trade_id))
    ids = [""] * len(trades)
    for rank, index in enumerate(ordered, start=1):
        ids[index] = f"T{rank:02d}"
    return ids


def _import_load(path: Path) -> list[FillRecord]:
    from thesistester.journal.tradesviz import load_tradesviz_executions

    frame = load_tradesviz_executions(path, profile=TRADESVIZ_EXECUTIONS_PROFILE)
    return [_fill_from_pandas_row(row) for row in frame.to_dict(orient="records")]


def _import_pair(fills: list[FillRecord], *, include_manual: bool) -> list[JournalTrade]:
    import pandas as pd
    from thesistester.journal.pair import pair_journal_trades
    from thesistester.journal.schema import FILL_RECORD_COLUMNS as TT_FILL_COLS

    if not fills:
        frame = pd.DataFrame(columns=list(TT_FILL_COLS))
        frame["timestamp"] = pd.Series(dtype="datetime64[ns, UTC]")
    else:
        rows = [fill_to_dict(fill) for fill in fills]
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    paired = pair_journal_trades(frame, include_manual=include_manual)
    return [_trade_from_pandas_row(row) for row in paired.to_dict(orient="records")]


def _fill_from_pandas_row(row: dict[str, Any]) -> FillRecord:
    ts = row["timestamp"]
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    session = _as_date(row["session_date"])
    tags = row.get("tags") or ()
    flags = row.get("flags") or ()
    qty = row.get("qty")
    if qty is not None and hasattr(qty, "item"):
        qty = qty.item()
    year = row.get("contract_year")
    if year is not None and hasattr(year, "item"):
        year = year.item()
    return FillRecord(
        fill_id=str(row["fill_id"]),
        source="tradesviz",
        source_group_id=_none_if_nan(row.get("source_group_id")),
        instrument=str(row["instrument"]),
        contract_month=_none_if_nan(row.get("contract_month")),
        contract_year=None if year is None or _is_nan(year) else int(year),
        side=str(row["side"]),
        qty=None if qty is None or _is_nan(qty) else int(qty),
        price=float(row["price"]),
        timestamp=ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc),
        session_date=session,
        entry_kind=str(row["entry_kind"]),
        tags=tuple(tags),
        notes_text="" if row.get("notes_text") is None else str(row["notes_text"]),
        declared_stop=_opt_float(row.get("declared_stop")),
        declared_target=_opt_float(row.get("declared_target")),
        flags=tuple(flags),
    )


def _trade_from_pandas_row(row: dict[str, Any]) -> JournalTrade:
    def _ts(value: Any) -> datetime | None:
        if value is None or _is_nan(value):
            return None
        if hasattr(value, "to_pydatetime"):
            value = value.to_pydatetime()
        if getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    session = _as_date(row["session_date"])
    tags = row.get("tags") or ()
    year = row.get("contract_year")
    return JournalTrade(
        trade_id=str(row["trade_id"]),
        source_group_id=_none_if_nan(row.get("source_group_id")),
        pair_method=str(row["pair_method"]),  # type: ignore[arg-type]
        lot_seq=int(row["lot_seq"]),
        direction=str(row["direction"]),  # type: ignore[arg-type]
        instrument=str(row["instrument"]),
        contract_month=_none_if_nan(row.get("contract_month")),
        contract_year=None if year is None or _is_nan(year) else int(year),
        session_date=session,
        qty=int(row["qty"]),
        entry_timestamp=_ts(row["entry_timestamp"]),  # type: ignore[arg-type]
        exit_timestamp=_ts(row.get("exit_timestamp")),
        entry_price=float(row["entry_price"]),
        exit_price=_opt_float(row.get("exit_price")),
        entry_fill_id=str(row["entry_fill_id"]),
        exit_fill_id=_none_if_nan(row.get("exit_fill_id")),
        gross_pnl_points=_opt_float(row.get("gross_pnl_points")),
        gross_pnl_currency=_opt_float(row.get("gross_pnl_currency")),
        commission_cost=_opt_float(row.get("commission_cost")),
        slippage_cost=_opt_float(row.get("slippage_cost")),
        day_fee_allocation=_opt_float(row.get("day_fee_allocation")),
        net_pnl_currency=_opt_float(row.get("net_pnl_currency")),
        r_multiple=_opt_float(row.get("r_multiple")),
        r_multiple_declared=_opt_float(row.get("r_multiple_declared")),
        journal_risk_ticks=int(row["journal_risk_ticks"]),
        fee_ticks=_opt_float(row.get("fee_ticks")),
        net_ticks=_opt_float(row.get("net_ticks")),
        hold_seconds=_opt_float(row.get("hold_seconds")),
        bars_held=None if row.get("bars_held") is None or _is_nan(row.get("bars_held")) else int(row["bars_held"]),
        mae_points=_opt_float(row.get("mae_points")),
        mfe_points=_opt_float(row.get("mfe_points")),
        stop_price=_opt_float(row.get("stop_price")),
        target_price=_opt_float(row.get("target_price")),
        tags=tuple(tags),
        notes_text="" if row.get("notes_text") is None else str(row["notes_text"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        signal_id=_none_if_nan(row.get("signal_id")),
        trigger=_none_if_nan(row.get("trigger")),
    )


def _is_nan(value: Any) -> bool:
    if value is None:
        return False
    try:
        return bool(value != value)  # NaN / NaT
    except (TypeError, ValueError):
        return type(value).__name__ in {"NAType", "NaTType"}


def _as_date(value: Any) -> date:
    if value is None or _is_nan(value):
        raise FillsError("fill session_date must be a calendar date")
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise FillsError(f"fill session_date must be a calendar date (got {value!r})")


def _none_if_nan(value: Any) -> str | None:
    if value is None or _is_nan(value) or value == "":
        return None
    return str(value)


def _opt_float(value: Any) -> float | None:
    if value is None or _is_nan(value):
        return None
    return float(value)


def load_fills(
    executions: Path,
    *,
    prefer_import: bool | None = None,
) -> tuple[str, list[FillRecord]]:
    use_import = thesistester_available() if prefer_import is None else prefer_import
    if use_import:
        if not thesistester_available():
            raise FillsError("ThesisTester is not installed (pip install 'tradevidanalyser[journal]')")
        return "import", _import_load(executions)
    return "mirror", mirror_load(executions, profile=TRADESVIZ_EXECUTIONS_PROFILE)


def pair_fills(
    fills: list[FillRecord],
    *,
    include_manual: bool = False,
    loader: str = "mirror",
) -> list[JournalTrade]:
    if loader == "import":
        return _import_pair(fills, include_manual=include_manual)
    return mirror_pair(fills, include_manual=include_manual)


def load_and_pair(
    executions: Path,
    *,
    include_manual: bool = False,
    prefer_import: bool | None = None,
) -> tuple[str, list[FillRecord], list[JournalTrade]]:
    loader, fills = load_fills(executions, prefer_import=prefer_import)
    return loader, fills, pair_fills(fills, include_manual=include_manual, loader=loader)


def _string_list(values: tuple[str, ...]) -> list[str]:
    return list(values)


def fills_table(fills: list[FillRecord], *, venue: str) -> pa.Table:
    rows = [fill_to_dict(fill) for fill in fills]
    for row in rows:
        row["venue"] = venue
        row["tags"] = _string_list(row["tags"])
        row["flags"] = _string_list(row["flags"])
        row["timestamp"] = row["timestamp"].astimezone(timezone.utc)
    arrays: dict[str, pa.Array] = {}
    arrays["fill_id"] = pa.array([row["fill_id"] for row in rows], type=pa.string())
    arrays["source"] = pa.array([row["source"] for row in rows], type=pa.string())
    arrays["source_group_id"] = pa.array([row["source_group_id"] for row in rows], type=pa.string())
    arrays["instrument"] = pa.array([row["instrument"] for row in rows], type=pa.string())
    arrays["contract_month"] = pa.array([row["contract_month"] for row in rows], type=pa.string())
    arrays["contract_year"] = pa.array([row["contract_year"] for row in rows], type=pa.int64())
    arrays["side"] = pa.array([row["side"] for row in rows], type=pa.string())
    arrays["qty"] = pa.array([row["qty"] for row in rows], type=pa.int64())
    arrays["price"] = pa.array([row["price"] for row in rows], type=pa.float64())
    arrays["timestamp"] = pa.array(
        [row["timestamp"] for row in rows], type=pa.timestamp("us", tz="UTC")
    )
    arrays["session_date"] = pa.array([row["session_date"] for row in rows], type=pa.date32())
    arrays["entry_kind"] = pa.array([row["entry_kind"] for row in rows], type=pa.string())
    arrays["tags"] = pa.array([row["tags"] for row in rows], type=pa.list_(pa.string()))
    arrays["notes_text"] = pa.array([row["notes_text"] for row in rows], type=pa.string())
    arrays["declared_stop"] = pa.array([row["declared_stop"] for row in rows], type=pa.float64())
    arrays["declared_target"] = pa.array([row["declared_target"] for row in rows], type=pa.float64())
    arrays["flags"] = pa.array([row["flags"] for row in rows], type=pa.list_(pa.string()))
    arrays["venue"] = pa.array([row["venue"] for row in rows], type=pa.string())
    table = pa.table(arrays)
    return table.select(list(TVA_FILL_COLUMNS)) if rows else _empty_fills_table()


def trades_table(trades: list[JournalTrade], *, venue: str) -> pa.Table:
    ids = assign_tva_trade_ids(trades)
    rows = [trade_to_dict(trade) for trade in trades]
    for row, tva_id in zip(rows, ids, strict=True):
        row["venue"] = venue
        row["tva_trade_id"] = tva_id
        row["tags"] = _string_list(row["tags"])
        if row["entry_timestamp"] is not None:
            row["entry_timestamp"] = row["entry_timestamp"].astimezone(timezone.utc)
        if row["exit_timestamp"] is not None:
            row["exit_timestamp"] = row["exit_timestamp"].astimezone(timezone.utc)
    if not rows:
        return _empty_trades_table()
    arrays: dict[str, pa.Array] = {}
    for name, typ in (
        ("trade_id", pa.string()),
        ("source_group_id", pa.string()),
        ("pair_method", pa.string()),
        ("lot_seq", pa.int64()),
        ("direction", pa.string()),
        ("instrument", pa.string()),
        ("contract_month", pa.string()),
        ("contract_year", pa.int64()),
        ("session_date", pa.date32()),
        ("qty", pa.int64()),
        ("entry_timestamp", pa.timestamp("us", tz="UTC")),
        ("exit_timestamp", pa.timestamp("us", tz="UTC")),
        ("entry_price", pa.float64()),
        ("exit_price", pa.float64()),
        ("entry_fill_id", pa.string()),
        ("exit_fill_id", pa.string()),
        ("gross_pnl_points", pa.float64()),
        ("gross_pnl_currency", pa.float64()),
        ("commission_cost", pa.float64()),
        ("slippage_cost", pa.float64()),
        ("day_fee_allocation", pa.float64()),
        ("net_pnl_currency", pa.float64()),
        ("r_multiple", pa.float64()),
        ("r_multiple_declared", pa.float64()),
        ("journal_risk_ticks", pa.int64()),
        ("fee_ticks", pa.float64()),
        ("net_ticks", pa.float64()),
        ("hold_seconds", pa.float64()),
        ("bars_held", pa.int64()),
        ("mae_points", pa.float64()),
        ("mfe_points", pa.float64()),
        ("stop_price", pa.float64()),
        ("target_price", pa.float64()),
        ("tags", pa.list_(pa.string())),
        ("notes_text", pa.string()),
        ("status", pa.string()),
        ("signal_id", pa.string()),
        ("trigger", pa.string()),
        ("venue", pa.string()),
        ("tva_trade_id", pa.string()),
    ):
        arrays[name] = pa.array([row[name] for row in rows], type=typ)
    return pa.table(arrays).select(list(TVA_TRADE_COLUMNS))


def _empty_fills_table() -> pa.Table:
    return pa.table(
        {
            "fill_id": pa.array([], type=pa.string()),
            "source": pa.array([], type=pa.string()),
            "source_group_id": pa.array([], type=pa.string()),
            "instrument": pa.array([], type=pa.string()),
            "contract_month": pa.array([], type=pa.string()),
            "contract_year": pa.array([], type=pa.int64()),
            "side": pa.array([], type=pa.string()),
            "qty": pa.array([], type=pa.int64()),
            "price": pa.array([], type=pa.float64()),
            "timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
            "session_date": pa.array([], type=pa.date32()),
            "entry_kind": pa.array([], type=pa.string()),
            "tags": pa.array([], type=pa.list_(pa.string())),
            "notes_text": pa.array([], type=pa.string()),
            "declared_stop": pa.array([], type=pa.float64()),
            "declared_target": pa.array([], type=pa.float64()),
            "flags": pa.array([], type=pa.list_(pa.string())),
            "venue": pa.array([], type=pa.string()),
        }
    ).select(list(TVA_FILL_COLUMNS))


def _empty_trades_table() -> pa.Table:
    empty = {
        "trade_id": pa.array([], type=pa.string()),
        "source_group_id": pa.array([], type=pa.string()),
        "pair_method": pa.array([], type=pa.string()),
        "lot_seq": pa.array([], type=pa.int64()),
        "direction": pa.array([], type=pa.string()),
        "instrument": pa.array([], type=pa.string()),
        "contract_month": pa.array([], type=pa.string()),
        "contract_year": pa.array([], type=pa.int64()),
        "session_date": pa.array([], type=pa.date32()),
        "qty": pa.array([], type=pa.int64()),
        "entry_timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
        "exit_timestamp": pa.array([], type=pa.timestamp("us", tz="UTC")),
        "entry_price": pa.array([], type=pa.float64()),
        "exit_price": pa.array([], type=pa.float64()),
        "entry_fill_id": pa.array([], type=pa.string()),
        "exit_fill_id": pa.array([], type=pa.string()),
        "gross_pnl_points": pa.array([], type=pa.float64()),
        "gross_pnl_currency": pa.array([], type=pa.float64()),
        "commission_cost": pa.array([], type=pa.float64()),
        "slippage_cost": pa.array([], type=pa.float64()),
        "day_fee_allocation": pa.array([], type=pa.float64()),
        "net_pnl_currency": pa.array([], type=pa.float64()),
        "r_multiple": pa.array([], type=pa.float64()),
        "r_multiple_declared": pa.array([], type=pa.float64()),
        "journal_risk_ticks": pa.array([], type=pa.int64()),
        "fee_ticks": pa.array([], type=pa.float64()),
        "net_ticks": pa.array([], type=pa.float64()),
        "hold_seconds": pa.array([], type=pa.float64()),
        "bars_held": pa.array([], type=pa.int64()),
        "mae_points": pa.array([], type=pa.float64()),
        "mfe_points": pa.array([], type=pa.float64()),
        "stop_price": pa.array([], type=pa.float64()),
        "target_price": pa.array([], type=pa.float64()),
        "tags": pa.array([], type=pa.list_(pa.string())),
        "notes_text": pa.array([], type=pa.string()),
        "status": pa.array([], type=pa.string()),
        "signal_id": pa.array([], type=pa.string()),
        "trigger": pa.array([], type=pa.string()),
        "venue": pa.array([], type=pa.string()),
        "tva_trade_id": pa.array([], type=pa.string()),
    }
    return pa.table(empty).select(list(TVA_TRADE_COLUMNS))


def write_table(path: Path, table: pa.Table) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    pq.write_table(table, tmp, compression="none")
    tmp.replace(path)
    return path


def contract_fills_table(fills: list[FillRecord]) -> pa.Table:
    """ThesisTester FILL_RECORD_COLUMNS only (no TVA venue)."""
    return fills_table(fills, venue="_").drop(["venue"])


def contract_trades_table(trades: list[JournalTrade]) -> pa.Table:
    """ThesisTester JOURNAL_TRADE_COLUMNS only (no venue / tva_trade_id)."""
    return trades_table(trades, venue="_").drop(["venue", "tva_trade_id"])


def assert_no_cost_columns(table: pa.Table) -> None:
    present = set(table.column_names)
    leaked = [name for name in FORBIDDEN_COST_COLUMNS if name in present]
    if leaked:
        raise FillsError("cost columns leaked into parquet: " + ", ".join(leaked))


def ingest_fills(
    record: SessionRecord,
    executions: Path,
    *,
    root: Path,
    venue: str | None = None,
    include_manual: bool = False,
    prefer_import: bool | None = None,
) -> FillsResult:
    csv_path = Path(executions)
    chosen_venue = venue_from_hint(csv_path, venue)
    try:
        loader, fills = load_fills(csv_path, prefer_import=prefer_import)
    except Exception as exc:
        if _is_journal_ingest_error(exc):
            raise FillsError(str(exc)) from exc
        raise
    start, end = session_utc_window(record)
    windowed = filter_fills_to_window(fills, start, end)
    fills_file = store.fills_path(root, record.id)
    trades_file = store.trades_path(root, record.id)
    if not windowed:
        fills_file.unlink(missing_ok=True)
        trades_file.unlink(missing_ok=True)
        store.compute_status(root, record.id)
        return FillsResult(
            session_id=record.id,
            loader=loader,
            venue=chosen_venue,
            fills=0,
            trades=0,
            path="",
            trades_path="",
            include_manual=include_manual,
            status="skipped",
            reason="no fills in session window",
        )
    try:
        trades = pair_fills(windowed, include_manual=include_manual, loader=loader)
    except Exception as exc:
        if _is_journal_ingest_error(exc):
            raise FillsError(str(exc)) from exc
        raise
    fill_table = fills_table(windowed, venue=chosen_venue)
    trade_table = trades_table(trades, venue=chosen_venue)
    assert_no_cost_columns(fill_table)
    assert_no_cost_columns(trade_table)
    write_table(fills_file, fill_table)
    write_table(trades_file, trade_table)
    store.compute_status(root, record.id)
    return FillsResult(
        session_id=record.id,
        loader=loader,
        venue=chosen_venue,
        fills=len(windowed),
        trades=len(trades),
        path="fills.parquet",
        trades_path="trades.parquet",
        include_manual=include_manual,
    )
