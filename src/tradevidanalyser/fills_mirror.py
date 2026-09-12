"""Mirror of ThesisTester ``tradesviz_executions`` load + FIFO pair (TJ1/TJ3).

Used when the ``tradevidanalyser[journal]`` extra is not installed. Must
produce the same ``FILL_RECORD_COLUMNS`` / ``JOURNAL_TRADE_COLUMNS`` frames
as ``thesistester.journal.tradesviz.load_tradesviz_executions`` and
``thesistester.journal.pair.pair_journal_trades``. ``commission`` / ``fees``
are read then discarded (TJ1).
"""

from __future__ import annotations

import csv
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, fields
from datetime import date, datetime, time, timedelta, timezone
from html import unescape
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

TRADESVIZ_EXECUTIONS_PROFILE = "tradesviz_executions"
JOURNAL_ETH_START = time(18, 0)
JOURNAL_EXCHANGE_TZ = ZoneInfo("America/New_York")
SOURCE_TRADESVIZ = "tradesviz"
ENTRY_KIND_IMPORTED = "imported"
ENTRY_KIND_MANUAL = "manual"
FLAG_MANUAL_NO_QTY = "manual_no_qty"
PAIR_METHOD_SPREAD = "spread_id"
PAIR_METHOD_FIFO = "fifo_fallback"
STATUS_CLOSED = "closed"
STATUS_OPEN = "open"
DEFAULT_JOURNAL_RISK_TICKS = 10
JOURNAL_TICK_SIZE = 0.25
JOURNAL_POINT_VALUE = {"MNQ": 2.0, "MES": 5.0}

CME_MONTH_CODES = {
    "F": "JAN",
    "G": "FEB",
    "H": "MAR",
    "J": "APR",
    "K": "MAY",
    "M": "JUN",
    "N": "JUL",
    "Q": "AUG",
    "U": "SEP",
    "V": "OCT",
    "X": "NOV",
    "Z": "DEC",
}

_REQUIRED_COLUMNS = (
    "date",
    "symbol",
    "side",
    "currency",
    "underlying",
    "asset_type",
    "price",
    "quantity",
    "commission",
    "fees",
    "stop_loss",
    "profit_target",
    "tags",
    "notes",
    "spread_id",
)
_KNOWN_ROOTS = frozenset({"MNQ", "MES"})
_SIDES = frozenset({"buy", "sell"})
_NA_TOKENS = frozenset({"", "n/a", "na", "none", "null"})
_CONTRACT_RE = re.compile(r"^(?P<root>MNQ|MES)(?P<month>[FGHJKMNQUVXZ])(?P<yy>\d{2})$")
_IMG_RE = re.compile(r"<img\b[^>]*>", flags=re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ISO_TS_RE = re.compile(
    r"^(?P<head>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?)"
    r"(?P<off>Z|[+-]\d{2}:?\d{2})$"
)
_NAIVE_ISO_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?$")


class JournalIngestError(ValueError):
    """Same class name as ThesisTester so error types stay familiar."""


@dataclass(frozen=True)
class FillRecord:
    fill_id: str
    source: Literal["tradesviz"]
    source_group_id: str | None
    instrument: str
    contract_month: str | None
    contract_year: int | None
    side: Literal["buy", "sell"]
    qty: int | None
    price: float
    timestamp: datetime
    session_date: date
    entry_kind: Literal["imported", "manual"]
    tags: tuple[str, ...]
    notes_text: str
    declared_stop: float | None
    declared_target: float | None
    flags: tuple[str, ...]


FILL_RECORD_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(FillRecord))


@dataclass(frozen=True)
class JournalTrade:
    trade_id: str
    source_group_id: str | None
    pair_method: Literal["spread_id", "fifo_fallback"]
    lot_seq: int
    direction: Literal["long", "short"]
    instrument: str
    contract_month: str | None
    contract_year: int | None
    session_date: date
    qty: int
    entry_timestamp: datetime
    exit_timestamp: datetime | None
    entry_price: float
    exit_price: float | None
    entry_fill_id: str
    exit_fill_id: str | None
    gross_pnl_points: float | None
    gross_pnl_currency: float | None
    commission_cost: float | None
    slippage_cost: float | None
    day_fee_allocation: float | None
    net_pnl_currency: float | None
    r_multiple: float | None
    r_multiple_declared: float | None
    journal_risk_ticks: int
    fee_ticks: float | None
    net_ticks: float | None
    hold_seconds: float | None
    bars_held: int | None
    mae_points: float | None
    mfe_points: float | None
    stop_price: float | None
    target_price: float | None
    tags: tuple[str, ...]
    notes_text: str
    status: Literal["closed", "open"]
    signal_id: str | None
    trigger: str | None


JOURNAL_TRADE_COLUMNS: tuple[str, ...] = tuple(f.name for f in fields(JournalTrade))


def trading_session_date(ts_utc: datetime) -> date:
    """ETH 18:00 America/New_York — same rule as ThesisTester ``trading_session_date``."""
    local = ts_utc.astimezone(JOURNAL_EXCHANGE_TZ)
    day = local.date()
    if local.time() >= JOURNAL_ETH_START:
        day = day + timedelta(days=1)
    return day


def load_tradesviz_executions(path: str | Path, *, profile: str) -> list[FillRecord]:
    if profile != TRADESVIZ_EXECUTIONS_PROFILE:
        raise JournalIngestError(
            f"unsupported journal profile {profile!r}; "
            f"expected {TRADESVIZ_EXECUTIONS_PROFILE!r} (no autodetect)"
        )
    csv_path = Path(path)
    if not csv_path.is_file():
        raise JournalIngestError(f"TradesViz executions file not found: {csv_path}")
    raw_rows = _read_csv_rows(csv_path)
    records = [_row_to_record(index, row) for index, row in enumerate(raw_rows)]
    filled: list[FillRecord] = []
    for record in records:
        filled.append(
            FillRecord(
                fill_id=record.fill_id,
                source=record.source,
                source_group_id=record.source_group_id,
                instrument=record.instrument,
                contract_month=record.contract_month,
                contract_year=record.contract_year,
                side=record.side,
                qty=record.qty,
                price=record.price,
                timestamp=record.timestamp,
                session_date=trading_session_date(record.timestamp),
                entry_kind=record.entry_kind,
                tags=record.tags,
                notes_text=record.notes_text,
                declared_stop=record.declared_stop,
                declared_target=record.declared_target,
                flags=record.flags,
            )
        )
    return filled


def _read_csv_rows(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise JournalIngestError("TradesViz executions CSV has no header")
        header = _normalized_header(reader.fieldnames)
        _require_columns(header)
        return [_remap_row(raw) for raw in reader]


def _normalized_header(fieldnames: list[str | None]) -> tuple[str, ...]:
    raw: list[str] = []
    for name in fieldnames:
        if name is None:
            raise JournalIngestError("TradesViz executions CSV has an unnamed header")
        raw.append(name.strip())
    while raw and raw[-1] == "":
        raw.pop()
    if any(name == "" for name in raw):
        raise JournalIngestError("TradesViz executions CSV has a blank header name")
    header = tuple(raw)
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in header:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise JournalIngestError(
            "TradesViz executions CSV has duplicate columns: " + ", ".join(duplicates)
        )
    return header


def _remap_row(raw: Mapping[str | None, str | None]) -> dict[str, str]:
    row: dict[str, str] = {}
    for key, value in raw.items():
        if key is None or not str(key).strip():
            if value not in (None, ""):
                raise JournalIngestError(
                    "TradesViz executions CSV has extra fields without a header"
                )
            continue
        row[key.strip()] = "" if value is None else str(value)
    return row


def _require_columns(header: tuple[str, ...]) -> None:
    present = set(header)
    missing = [name for name in _REQUIRED_COLUMNS if name not in present]
    if missing:
        raise JournalIngestError(
            "TradesViz executions CSV missing required columns: " + ", ".join(missing)
        )


def _row_to_record(index: int, row: Mapping[str, str | None]) -> FillRecord:
    timestamp = _parse_timestamp(_cell(row, "date"), index=index)
    symbol = _cell(row, "symbol")
    instrument, contract_month, contract_year = _parse_symbol(symbol, index=index)
    side = _parse_side(_cell(row, "side"), index=index)
    asset_type = _cell(row, "asset_type").strip().lower()
    if not asset_type:
        raise JournalIngestError(f"row {index}: empty asset_type")
    entry_kind = ENTRY_KIND_IMPORTED if asset_type == "future" else ENTRY_KIND_MANUAL
    if entry_kind == ENTRY_KIND_IMPORTED and (contract_month is None or contract_year is None):
        raise JournalIngestError(
            f"row {index}: imported fill requires a CME month-year symbol "
            f"(got {symbol!r}; bare MNQ/MES is the manual pattern)"
        )
    qty, flags = _parse_qty(_cell(row, "quantity"), entry_kind=entry_kind, index=index)
    price = _parse_price(_cell(row, "price"), index=index)
    _cell(row, "commission")
    _cell(row, "fees")
    _cell(row, "currency")
    _cell(row, "underlying")
    declared_stop = _parse_optional_float(_cell(row, "stop_loss"), index=index)
    declared_target = _parse_optional_float(_cell(row, "profit_target"), index=index)
    tags = _parse_tags(_cell(row, "tags"))
    notes_text = strip_notes_html(_cell(row, "notes"))
    source_group_id = _optional_text(_cell(row, "spread_id"))
    fill_id = _fill_id(
        index,
        source_group_id=source_group_id,
        timestamp=timestamp,
        side=side,
        price=price,
        qty=qty,
    )
    return FillRecord(
        fill_id=fill_id,
        source=SOURCE_TRADESVIZ,
        source_group_id=source_group_id,
        instrument=instrument,
        contract_month=contract_month,
        contract_year=contract_year,
        side=side,
        qty=qty,
        price=price,
        timestamp=timestamp,
        session_date=timestamp.date(),
        entry_kind=entry_kind,
        tags=tags,
        notes_text=notes_text,
        declared_stop=declared_stop,
        declared_target=declared_target,
        flags=flags,
    )


def _fill_id(
    index: int,
    *,
    source_group_id: str | None,
    timestamp: datetime,
    side: str,
    price: float,
    qty: int | None,
) -> str:
    group = source_group_id if source_group_id is not None else "-"
    qty_part = "-" if qty is None else str(qty)
    ts = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"tv:{index:06d}:{group}:{ts}:{side}:{price}:{qty_part}"


def _cell(row: Mapping[str, str | None], name: str) -> str:
    if name not in row:
        raise JournalIngestError(f"missing column {name!r}")
    value = row[name]
    return "" if value is None else str(value)


def _parse_timestamp(raw: str, *, index: int) -> datetime:
    text = raw.strip()
    if not text:
        raise JournalIngestError(f"row {index}: empty date")
    if _NAIVE_ISO_TS_RE.fullmatch(text):
        raise JournalIngestError(
            f"row {index}: date must carry an explicit UTC offset (got naive {raw!r})"
        )
    match = _ISO_TS_RE.fullmatch(text)
    if match is None:
        raise JournalIngestError(
            f"row {index}: date must be ISO-8601 with an explicit offset (got {raw!r})"
        )
    head = match.group("head").replace(" ", "T")
    off = match.group("off")
    if off == "Z":
        normalized = f"{head}+00:00"
    elif ":" not in off:
        normalized = f"{head}{off[:-2]}:{off[-2:]}"
    else:
        normalized = f"{head}{off}"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise JournalIngestError(f"row {index}: unparseable date {raw!r}") from exc
    if parsed.tzinfo is None:
        raise JournalIngestError(
            f"row {index}: date must carry an explicit UTC offset (got naive {raw!r})"
        )
    return parsed.astimezone(timezone.utc)


def _parse_symbol(raw: str, *, index: int) -> tuple[str, str | None, int | None]:
    symbol = raw.strip().upper()
    if symbol in _KNOWN_ROOTS:
        return symbol, None, None
    match = _CONTRACT_RE.fullmatch(symbol)
    if match is None:
        raise JournalIngestError(f"row {index}: unknown symbol {raw!r}")
    return match.group("root"), CME_MONTH_CODES[match.group("month")], 2000 + int(match.group("yy"))


def _parse_side(raw: str, *, index: int) -> str:
    side = raw.strip().lower()
    if side not in _SIDES:
        raise JournalIngestError(f"row {index}: side must be buy or sell (got {raw!r})")
    return side


def _parse_qty(raw: str, *, entry_kind: str, index: int) -> tuple[int | None, tuple[str, ...]]:
    text = raw.strip()
    if not text:
        raise JournalIngestError(f"row {index}: empty quantity")
    try:
        value = float(text)
    except ValueError as exc:
        raise JournalIngestError(f"row {index}: unparseable quantity {raw!r}") from exc
    if value == 0.0:
        if entry_kind != ENTRY_KIND_MANUAL:
            raise JournalIngestError(
                f"row {index}: imported fill quantity must be a positive integer"
            )
        return None, (FLAG_MANUAL_NO_QTY,)
    if value < 0 or not value.is_integer():
        raise JournalIngestError(f"row {index}: quantity must be a positive integer (got {raw!r})")
    return int(value), ()


def _parse_price(raw: str, *, index: int) -> float:
    text = raw.strip()
    if not text:
        raise JournalIngestError(f"row {index}: empty price")
    try:
        value = float(text)
    except ValueError as exc:
        raise JournalIngestError(f"row {index}: unparseable price {raw!r}") from exc
    if not math.isfinite(value) or value <= 0.0:
        raise JournalIngestError(
            f"row {index}: price must be a finite positive number (got {raw!r})"
        )
    return value


def _parse_optional_float(raw: str, *, index: int) -> float | None:
    text = raw.strip()
    if text.lower() in _NA_TOKENS:
        return None
    try:
        value = float(text)
    except ValueError as exc:
        raise JournalIngestError(f"row {index}: unparseable optional numeric {raw!r}") from exc
    if not math.isfinite(value):
        raise JournalIngestError(f"row {index}: optional numeric must be finite (got {raw!r})")
    return value


def _parse_tags(raw: str) -> tuple[str, ...]:
    if not raw.strip():
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _optional_text(raw: str) -> str | None:
    text = raw.strip()
    return text if text else None


def strip_notes_html(raw: str) -> str:
    if not raw:
        return ""
    text = _IMG_RE.sub("[image]", raw)
    text = _HTML_TAG_RE.sub(" ", text)
    text = unescape(text)
    return " ".join(text.split())


@dataclass(frozen=True)
class _Fill:
    fill_id: str
    source_group_id: str | None
    instrument: str
    contract_month: str | None
    contract_year: int | None
    side: str
    qty: int
    price: float
    timestamp: datetime
    session_date: date
    tags: tuple[str, ...]
    notes_text: str
    declared_stop: float | None
    declared_target: float | None


@dataclass
class _OpenLot:
    fill: _Fill
    remaining: int


@dataclass(frozen=True)
class _Intent:
    tags: tuple[str, ...]
    notes_text: str
    declared_stop: float | None
    declared_target: float | None


def pair_journal_trades(
    fills: Sequence[FillRecord] | Sequence[Mapping[str, Any]],
    *,
    include_manual: bool = False,
    journal_risk_ticks: int = DEFAULT_JOURNAL_RISK_TICKS,
) -> list[JournalTrade]:
    if (
        type(journal_risk_ticks) is not int
        or isinstance(journal_risk_ticks, bool)
        or journal_risk_ticks <= 0
    ):
        raise JournalIngestError(
            f"journal_risk_ticks must be a positive int (got {journal_risk_ticks!r})"
        )
    if not isinstance(include_manual, bool):
        raise JournalIngestError("include_manual must be a bool")
    rows = _fills_from_records(fills, include_manual=include_manual)
    grouped: dict[str, list[_Fill]] = defaultdict(list)
    fallback: list[_Fill] = []
    for fill in rows:
        if fill.source_group_id is None:
            fallback.append(fill)
            continue
        grouped[fill.source_group_id].append(fill)

    trades: list[JournalTrade] = []
    for spread_id, members in grouped.items():
        ordered = _sorted_fills(members)
        _assert_homogeneous_group(ordered, spread_id)
        intent = _intent_from_fills(ordered)
        if _nets_flat_one_open_side(ordered):
            closed, leftovers = _fifo_match(
                ordered,
                pair_method=PAIR_METHOD_SPREAD,
                group_key=spread_id,
                intent=intent,
                journal_risk_ticks=journal_risk_ticks,
            )
            trades.extend(closed)
            trades.extend(
                _open_trade(
                    lot=lot,
                    lot_seq=len(closed) + index,
                    pair_method=PAIR_METHOD_SPREAD,
                    group_key=spread_id,
                    intent=intent,
                    journal_risk_ticks=journal_risk_ticks,
                )
                for index, lot in enumerate(leftovers)
            )
        else:
            closed, leftovers = _fifo_match(
                ordered,
                pair_method=PAIR_METHOD_FIFO,
                group_key=spread_id,
                intent=intent,
                journal_risk_ticks=journal_risk_ticks,
            )
            trades.extend(closed)
            fallback.extend(_fill_from_lot(lot, intent=intent) for lot in leftovers)

    buckets: dict[tuple[str, str | None, int | None, date], list[_Fill]] = defaultdict(list)
    for fill in fallback:
        buckets[_fallback_key(fill)].append(fill)
    for key, members in buckets.items():
        ordered = _sorted_fills(members)
        trades.extend(
            _fifo_pair(
                ordered,
                pair_method=PAIR_METHOD_FIFO,
                group_key=_fifo_group_key(key),
                intent=None,
                journal_risk_ticks=journal_risk_ticks,
            )
        )

    trades.sort(key=lambda trade: (trade.entry_timestamp, trade.trade_id))
    return trades


def _as_record(item: FillRecord | Mapping[str, Any]) -> FillRecord:
    if isinstance(item, FillRecord):
        return item
    return FillRecord(**{name: item[name] for name in FILL_RECORD_COLUMNS})


def _fills_from_records(
    fills: Sequence[FillRecord] | Sequence[Mapping[str, Any]],
    *,
    include_manual: bool,
) -> list[_Fill]:
    out: list[_Fill] = []
    for item in fills:
        record = _as_record(item)
        if record.entry_kind != ENTRY_KIND_IMPORTED and not include_manual:
            continue
        if record.qty is None:
            continue
        if record.instrument not in JOURNAL_POINT_VALUE:
            raise JournalIngestError(f"unknown journal instrument {record.instrument!r}")
        out.append(
            _Fill(
                fill_id=record.fill_id,
                source_group_id=record.source_group_id,
                instrument=record.instrument,
                contract_month=record.contract_month,
                contract_year=record.contract_year,
                side=record.side,
                qty=int(record.qty),
                price=float(record.price),
                timestamp=record.timestamp,
                session_date=record.session_date,
                tags=tuple(record.tags),
                notes_text=record.notes_text,
                declared_stop=record.declared_stop,
                declared_target=record.declared_target,
            )
        )
    return out


def _sorted_fills(fills: Sequence[_Fill]) -> list[_Fill]:
    return sorted(fills, key=lambda fill: (fill.timestamp, fill.fill_id))


def _nets_flat_one_open_side(fills: Sequence[_Fill]) -> bool:
    if not fills:
        return False
    net = 0
    open_side: str | None = None
    buy_qty = 0
    sell_qty = 0
    for fill in fills:
        if fill.side == "buy":
            buy_qty += fill.qty
            net += fill.qty
        elif fill.side == "sell":
            sell_qty += fill.qty
            net -= fill.qty
        else:
            raise JournalIngestError(f"fill {fill.fill_id!r} side must be buy or sell")
        if net == 0:
            continue
        side = "buy" if net > 0 else "sell"
        if open_side is None:
            open_side = side
        elif side != open_side:
            return False
    return buy_qty == sell_qty and buy_qty > 0


def _intent_from_fills(fills: Sequence[_Fill]) -> _Intent:
    tags: list[str] = []
    seen: set[str] = set()
    notes = ""
    stop = None
    target = None
    for fill in fills:
        for tag in fill.tags:
            if tag not in seen:
                seen.add(tag)
                tags.append(tag)
        if not notes and fill.notes_text:
            notes = fill.notes_text
        if stop is None and fill.declared_stop is not None:
            stop = fill.declared_stop
        if target is None and fill.declared_target is not None:
            target = fill.declared_target
    return _Intent(tags=tuple(tags), notes_text=notes, declared_stop=stop, declared_target=target)


def _assert_homogeneous_group(fills: Sequence[_Fill], spread_id: str) -> None:
    instruments = {fill.instrument for fill in fills}
    months = {fill.contract_month for fill in fills}
    years = {fill.contract_year for fill in fills}
    if len(instruments) > 1 or len(months) > 1 or len(years) > 1:
        raise JournalIngestError(
            f"spread_id {spread_id!r} mixes instrument/contract "
            f"(instruments={sorted(instruments)}, months={sorted(str(m) for m in months)}, "
            f"years={sorted(str(y) for y in years)})"
        )


def _fill_from_lot(lot: _OpenLot, *, intent: _Intent) -> _Fill:
    fill = lot.fill
    return _Fill(
        fill_id=fill.fill_id,
        source_group_id=fill.source_group_id,
        instrument=fill.instrument,
        contract_month=fill.contract_month,
        contract_year=fill.contract_year,
        side=fill.side,
        qty=lot.remaining,
        price=fill.price,
        timestamp=fill.timestamp,
        session_date=fill.session_date,
        tags=intent.tags,
        notes_text=intent.notes_text,
        declared_stop=intent.declared_stop,
        declared_target=intent.declared_target,
    )


def _fallback_key(fill: _Fill) -> tuple[str, str | None, int | None, date]:
    return (fill.instrument, fill.contract_month, fill.contract_year, fill.session_date)


def _fifo_group_key(key: tuple[str, str | None, int | None, date]) -> str:
    instrument, month, year, session = key
    month_part = month if month is not None else "-"
    year_part = str(year) if year is not None else "-"
    return f"fifo:{instrument}:{month_part}:{year_part}:{session.isoformat()}"


def _fifo_match(
    fills: Sequence[_Fill],
    *,
    pair_method: str,
    group_key: str,
    intent: _Intent | None,
    journal_risk_ticks: int,
) -> tuple[list[JournalTrade], list[_OpenLot]]:
    opens: deque[_OpenLot] = deque()
    trades: list[JournalTrade] = []
    lot_seq = 0
    for fill in fills:
        remaining = fill.qty
        while remaining > 0:
            if not opens or opens[0].fill.side == fill.side:
                opens.append(_OpenLot(fill=fill, remaining=remaining))
                remaining = 0
                break
            lot = opens[0]
            take = min(lot.remaining, remaining)
            trade_intent = intent if intent is not None else _intent_from_fills((lot.fill, fill))
            trades.append(
                _closed_trade(
                    entry=lot.fill,
                    exit_fill=fill,
                    qty=take,
                    lot_seq=lot_seq,
                    pair_method=pair_method,
                    group_key=group_key,
                    intent=trade_intent,
                    journal_risk_ticks=journal_risk_ticks,
                )
            )
            lot_seq += 1
            lot.remaining -= take
            remaining -= take
            if lot.remaining == 0:
                opens.popleft()
    return trades, list(opens)


def _fifo_pair(
    fills: Sequence[_Fill],
    *,
    pair_method: str,
    group_key: str,
    intent: _Intent | None,
    journal_risk_ticks: int,
) -> list[JournalTrade]:
    trades, leftovers = _fifo_match(
        fills,
        pair_method=pair_method,
        group_key=group_key,
        intent=intent,
        journal_risk_ticks=journal_risk_ticks,
    )
    lot_seq = len(trades)
    for lot in leftovers:
        leftover_intent = intent if intent is not None else _intent_from_fills((lot.fill,))
        trades.append(
            _open_trade(
                lot=lot,
                lot_seq=lot_seq,
                pair_method=pair_method,
                group_key=group_key,
                intent=leftover_intent,
                journal_risk_ticks=journal_risk_ticks,
            )
        )
        lot_seq += 1
    return trades


def _closed_trade(
    *,
    entry: _Fill,
    exit_fill: _Fill,
    qty: int,
    lot_seq: int,
    pair_method: str,
    group_key: str,
    intent: _Intent,
    journal_risk_ticks: int,
) -> JournalTrade:
    direction: str = "long" if entry.side == "buy" else "short"
    points = exit_fill.price - entry.price if direction == "long" else entry.price - exit_fill.price
    point_value = JOURNAL_POINT_VALUE[entry.instrument]
    tick_value = JOURNAL_TICK_SIZE * point_value
    gross_currency = points * point_value * qty
    net_currency = gross_currency
    risk = journal_risk_ticks * tick_value * qty
    r_declared = None
    if intent.declared_stop is not None:
        distance = abs(entry.price - intent.declared_stop)
        if distance > 0:
            r_declared = net_currency / (distance * point_value * qty)
    hold = (exit_fill.timestamp - entry.timestamp).total_seconds()
    return JournalTrade(
        trade_id=f"jt:{group_key}:{lot_seq}",
        source_group_id=entry.source_group_id,
        pair_method=pair_method,  # type: ignore[arg-type]
        lot_seq=lot_seq,
        direction=direction,  # type: ignore[arg-type]
        instrument=entry.instrument,
        contract_month=entry.contract_month,
        contract_year=entry.contract_year,
        session_date=entry.session_date,
        qty=qty,
        entry_timestamp=entry.timestamp,
        exit_timestamp=exit_fill.timestamp,
        entry_price=entry.price,
        exit_price=exit_fill.price,
        entry_fill_id=entry.fill_id,
        exit_fill_id=exit_fill.fill_id,
        gross_pnl_points=points,
        gross_pnl_currency=gross_currency,
        commission_cost=None,
        slippage_cost=None,
        day_fee_allocation=None,
        net_pnl_currency=net_currency,
        r_multiple=net_currency / risk,
        r_multiple_declared=r_declared,
        journal_risk_ticks=journal_risk_ticks,
        fee_ticks=None,
        net_ticks=net_currency / tick_value,
        hold_seconds=hold,
        bars_held=None,
        mae_points=None,
        mfe_points=None,
        stop_price=intent.declared_stop,
        target_price=intent.declared_target,
        tags=intent.tags,
        notes_text=intent.notes_text,
        status=STATUS_CLOSED,
        signal_id=None,
        trigger=None,
    )


def _open_trade(
    *,
    lot: _OpenLot,
    lot_seq: int,
    pair_method: str,
    group_key: str,
    intent: _Intent,
    journal_risk_ticks: int,
) -> JournalTrade:
    entry = lot.fill
    direction: str = "long" if entry.side == "buy" else "short"
    return JournalTrade(
        trade_id=f"jt:{group_key}:{lot_seq}",
        source_group_id=entry.source_group_id,
        pair_method=pair_method,  # type: ignore[arg-type]
        lot_seq=lot_seq,
        direction=direction,  # type: ignore[arg-type]
        instrument=entry.instrument,
        contract_month=entry.contract_month,
        contract_year=entry.contract_year,
        session_date=entry.session_date,
        qty=lot.remaining,
        entry_timestamp=entry.timestamp,
        exit_timestamp=None,
        entry_price=entry.price,
        exit_price=None,
        entry_fill_id=entry.fill_id,
        exit_fill_id=None,
        gross_pnl_points=None,
        gross_pnl_currency=None,
        commission_cost=None,
        slippage_cost=None,
        day_fee_allocation=None,
        net_pnl_currency=None,
        r_multiple=None,
        r_multiple_declared=None,
        journal_risk_ticks=journal_risk_ticks,
        fee_ticks=None,
        net_ticks=None,
        hold_seconds=None,
        bars_held=None,
        mae_points=None,
        mfe_points=None,
        stop_price=intent.declared_stop,
        target_price=intent.declared_target,
        tags=intent.tags,
        notes_text=intent.notes_text,
        status=STATUS_OPEN,
        signal_id=None,
        trigger=None,
    )


def fill_to_dict(record: FillRecord) -> dict[str, Any]:
    return {name: getattr(record, name) for name in FILL_RECORD_COLUMNS}


def trade_to_dict(trade: JournalTrade) -> dict[str, Any]:
    return {name: getattr(trade, name) for name in JOURNAL_TRADE_COLUMNS}


def records_from_mapping_rows(rows: Iterable[Mapping[str, Any]]) -> list[FillRecord]:
    return [_as_record(row) for row in rows]
