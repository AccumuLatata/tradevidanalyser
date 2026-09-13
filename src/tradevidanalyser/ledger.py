"""DuckDB coaching ledger and weekly/monthly rollup (PR-24)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import duckdb

from tradevidanalyser import store
from tradevidanalyser.schema import (
    AdherenceTally,
    LedgerPeriod,
    LedgerSummary,
    RuleTally,
    StatedLabTally,
    ViolationTally,
)

SCHEMA_VERSION = "1"
LEDGER_FILENAME = "ledger.duckdb"
WEEK_RE = re.compile(r"^(\d{4})-W(\d{2})$")
MONTH_RE = re.compile(r"^(\d{4})-(\d{2})$")
TOKEN_RE = re.compile(r"[a-z0-9]+")


class LedgerError(ValueError):
    """Unreadable artifacts or a bad rollup window."""


@dataclass(frozen=True)
class LedgerAddResult:
    session_id: str
    status: str
    trades: int = 0
    rules: int = 0
    events: int = 0
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "session_id": self.session_id,
            "trades": self.trades,
            "rules": self.rules,
            "events": self.events,
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


@dataclass(frozen=True)
class RollupResult:
    status: str
    markdown: str
    summary: LedgerSummary

    def as_dict(self) -> dict[str, Any]:
        payload = self.summary.model_dump(mode="json")
        payload["status"] = self.status
        payload["markdown"] = self.markdown
        return payload


def ledger_dir(root: Path) -> Path:
    return root / "ledger"


def ledger_db_path(root: Path) -> Path:
    return ledger_dir(root) / LEDGER_FILENAME


def _require_safe_session_id(session_id: str) -> str:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return session_id


def _duck_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace("'", "''")


def _connect(root: Path, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = ledger_db_path(root)
    if read_only:
        if not path.is_file():
            raise LedgerError("ledger is empty")
        return duckdb.connect(str(path), read_only=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(path))
    _ensure_schema(con)
    return con


def _ensure_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            session_id VARCHAR PRIMARY KEY,
            session_date DATE,
            iso_week VARCHAR,
            year_month VARCHAR,
            start_wallclock_vienna VARCHAR,
            duration_s DOUBLE,
            language VARCHAR,
            trade_count INTEGER,
            hours DOUBLE
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS trades (
            session_id VARCHAR,
            tva_trade_id VARCHAR,
            trade_id VARCHAR,
            entry_fill_id VARCHAR,
            direction VARCHAR,
            instrument VARCHAR,
            entry_price DOUBLE,
            exit_price DOUBLE,
            net_pnl_currency DOUBLE,
            status VARCHAR,
            venue VARCHAR,
            stated_setup VARCHAR,
            stated_playbook VARCHAR,
            lab_token VARCHAR,
            tag_alignment VARCHAR,
            stated_lab_agree BOOLEAN,
            PRIMARY KEY (session_id, tva_trade_id)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS rule_checks (
            session_id VARCHAR,
            rule VARCHAR,
            status VARCHAR,
            reason VARCHAR,
            PRIMARY KEY (session_id, rule)
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            session_id VARCHAR,
            t DOUBLE,
            kind VARCHAR,
            seg VARCHAR,
            text VARCHAR
        )
        """
    )


def session_recorded(root: Path, session_id: str) -> bool:
    path = ledger_db_path(root)
    if not path.is_file():
        return False
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error:
        return False
    try:
        row = con.execute(
            "SELECT 1 FROM sessions WHERE session_id = ? LIMIT 1",
            [session_id],
        ).fetchone()
        return row is not None
    except duckdb.Error:
        return False
    finally:
        con.close()


def drop_session(root: Path, session_id: str) -> None:
    path = ledger_db_path(root)
    if not path.is_file():
        return
    con = duckdb.connect(str(path))
    try:
        _ensure_schema(con)
        con.execute("DELETE FROM events WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM rule_checks WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM trades WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM sessions WHERE session_id = ?", [session_id])
    finally:
        con.close()


def _session_date(session_id: str, wallclock: str) -> date:
    prefix = session_id[:10]
    try:
        return date.fromisoformat(prefix)
    except ValueError:
        pass
    if wallclock:
        try:
            return date.fromisoformat(wallclock[:10])
        except ValueError:
            pass
    raise LedgerError(f"cannot derive session date from {session_id!r}")


def iso_week_id(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def year_month_id(value: date) -> str:
    return f"{value.year:04d}-{value.month:02d}"


def parse_iso_week(raw: str) -> tuple[int, int]:
    match = WEEK_RE.fullmatch(raw.strip())
    if not match:
        raise LedgerError(f"invalid ISO week {raw!r} (expected YYYY-Www)")
    year = int(match.group(1))
    week = int(match.group(2))
    try:
        date.fromisocalendar(year, week, 1)
    except ValueError as exc:
        raise LedgerError(f"invalid ISO week {raw!r}") from exc
    return year, week


def parse_year_month(raw: str) -> tuple[int, int]:
    match = MONTH_RE.fullmatch(raw.strip())
    if not match:
        raise LedgerError(f"invalid month {raw!r} (expected YYYY-MM)")
    year = int(match.group(1))
    month = int(match.group(2))
    if month < 1 or month > 12:
        raise LedgerError(f"invalid month {raw!r}")
    return year, month


def shift_iso_week(week_id: str, delta: int) -> str:
    year, week = parse_iso_week(week_id)
    start = date.fromisocalendar(year, week, 1) + timedelta(weeks=delta)
    return iso_week_id(start)


def _norm_token(text: str) -> str:
    return "".join(TOKEN_RE.findall(text.casefold()))


def stated_lab_agree(
    stated_setup: str | None,
    stated_playbook: str | None,
    lab_token: str | None,
    tag_alignment: str | None,
) -> bool | None:
    stated = _norm_token(stated_setup or "") or _norm_token(stated_playbook or "")
    lab = _norm_token(lab_token or "")
    if stated and lab:
        return stated == lab or stated in lab or lab in stated
    if (tag_alignment or "").strip().casefold() == "all_aligned":
        return True
    return None


def _load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = store.read_json(path)
    return data if isinstance(data, dict) else {}


def _parquet_columns(con: duckdb.DuckDBPyConnection, path: Path) -> set[str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{_duck_path(path)}')").fetchall()
    return {str(row[0]) for row in rows}


def _sql_col(name: str, available: set[str], cast: str) -> str:
    if name in available:
        return f"CAST({name} AS {cast})"
    return "NULL"


def _read_trades_from_parquet(
    con: duckdb.DuckDBPyConnection, root: Path, session_id: str
) -> int:
    path = store.trades_path(root, session_id)
    if not path.is_file():
        return 0
    available = _parquet_columns(con, path)
    tva = _sql_col("tva_trade_id", available, "VARCHAR")
    trade_id = _sql_col("trade_id", available, "VARCHAR")
    src = _duck_path(path)
    con.execute(
        f"""
        INSERT INTO trades (
            session_id, tva_trade_id, trade_id, entry_fill_id, direction,
            instrument, entry_price, exit_price, net_pnl_currency, status, venue
        )
        SELECT
            ? AS session_id,
            COALESCE({tva}, {trade_id}) AS tva_trade_id,
            {trade_id} AS trade_id,
            {_sql_col("entry_fill_id", available, "VARCHAR")} AS entry_fill_id,
            {_sql_col("direction", available, "VARCHAR")} AS direction,
            {_sql_col("instrument", available, "VARCHAR")} AS instrument,
            {_sql_col("entry_price", available, "DOUBLE")} AS entry_price,
            {_sql_col("exit_price", available, "DOUBLE")} AS exit_price,
            {_sql_col("net_pnl_currency", available, "DOUBLE")} AS net_pnl_currency,
            {_sql_col("status", available, "VARCHAR")} AS status,
            {_sql_col("venue", available, "VARCHAR")} AS venue
        FROM read_parquet('{src}')
        WHERE COALESCE({tva}, {trade_id}) IS NOT NULL
          AND CAST(COALESCE({tva}, {trade_id}) AS VARCHAR) <> ''
        """,
        [session_id],
    )
    row = con.execute(
        "SELECT COUNT(*) FROM trades WHERE session_id = ?", [session_id]
    ).fetchone()
    return int(row[0]) if row else 0


def _apply_stated_lab(con: duckdb.DuckDBPyConnection, root: Path, session_id: str) -> None:
    evidence = _load_json(store.evidence_path(root, session_id))
    context = _load_json(store.context_path(root, session_id))
    lab_rows = {}
    lab = context.get("lab")
    if isinstance(lab, dict):
        per_trade = lab.get("per_trade")
        if isinstance(per_trade, dict):
            lab_rows = per_trade
    stated_by_id: dict[str, dict[str, str]] = {}
    for trade in evidence.get("trades") or []:
        if not isinstance(trade, dict):
            continue
        tva_id = str(trade.get("tva_trade_id") or "")
        if not tva_id:
            continue
        stated = trade.get("stated") if isinstance(trade.get("stated"), dict) else {}
        setup = stated.get("setup") if isinstance(stated.get("setup"), dict) else {}
        playbook = stated.get("playbook") if isinstance(stated.get("playbook"), dict) else {}
        stated_by_id[tva_id] = {
            "stated_setup": str(setup.get("value") or ""),
            "stated_playbook": str(playbook.get("value") or ""),
        }
    ids = [
        row[0]
        for row in con.execute(
            "SELECT tva_trade_id FROM trades WHERE session_id = ?", [session_id]
        ).fetchall()
    ]
    for tva_id in ids:
        stated = stated_by_id.get(str(tva_id), {})
        lab_row = lab_rows.get(str(tva_id)) if isinstance(lab_rows.get(str(tva_id)), dict) else {}
        setup = stated.get("stated_setup") or None
        playbook = stated.get("stated_playbook") or None
        token = str(lab_row.get("nearest_level_token") or "") or None
        align = str(lab_row.get("tag_alignment") or "") or None
        agree = stated_lab_agree(setup, playbook, token, align)
        con.execute(
            """
            UPDATE trades
            SET stated_setup = ?, stated_playbook = ?, lab_token = ?,
                tag_alignment = ?, stated_lab_agree = ?
            WHERE session_id = ? AND tva_trade_id = ?
            """,
            [setup, playbook, token, align, agree, session_id, tva_id],
        )


def _insert_rules(con: duckdb.DuckDBPyConnection, root: Path, session_id: str) -> int:
    data = _load_json(store.rules_path(root, session_id))
    count = 0
    for item in data.get("rules") or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "")
        status = str(item.get("status") or "")
        if not rule:
            continue
        reason = item.get("reason")
        con.execute(
            "INSERT INTO rule_checks VALUES (?, ?, ?, ?)",
            [session_id, rule, status, str(reason) if reason is not None else None],
        )
        count += 1
    return count


def _insert_events(con: duckdb.DuckDBPyConnection, root: Path, session_id: str) -> int:
    if not store.insights_path(root, session_id).is_file():
        return 0
    try:
        insights = store.load_insights(root, session_id)
    except (ValueError, OSError):
        return 0
    count = 0
    for event in insights.session_events:
        con.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            [session_id, float(event.t), event.kind, event.seg, event.text],
        )
        count += 1
    return count


def add_session(session_id: str, *, root: Path) -> LedgerAddResult:
    session_id = _require_safe_session_id(session_id)
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    day = _session_date(session_id, record.recording.start_wallclock_vienna)
    hours = (record.recording.duration_s or 0.0) / 3600.0
    con = _connect(root)
    try:
        con.execute("DELETE FROM events WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM rule_checks WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM trades WHERE session_id = ?", [session_id])
        con.execute("DELETE FROM sessions WHERE session_id = ?", [session_id])
        trades = _read_trades_from_parquet(con, root, session_id)
        _apply_stated_lab(con, root, session_id)
        rules = _insert_rules(con, root, session_id)
        events = _insert_events(con, root, session_id)
        con.execute(
            """
            INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                session_id,
                day,
                iso_week_id(day),
                year_month_id(day),
                record.recording.start_wallclock_vienna,
                float(record.recording.duration_s or 0.0),
                record.language,
                trades,
                hours,
            ],
        )
    finally:
        con.close()
    store.compute_status(root, session_id)
    return LedgerAddResult(
        session_id=session_id,
        status="ok",
        trades=trades,
        rules=rules,
        events=events,
    )


def _rate(passed: int, violated: int) -> float | None:
    den = passed + violated
    if den == 0:
        return None
    return passed / den


def _tph(trades: int, hours: float) -> float | None:
    if hours <= 0:
        return None
    return trades / hours


def _empty_adherence() -> AdherenceTally:
    return AdherenceTally()


def _tally_rules(rows: list[tuple[Any, ...]]) -> AdherenceTally:
    by_rule: dict[str, RuleTally] = {}
    passed = violated = unverifiable = 0
    for rule, status, count in rows:
        n = int(count)
        tally = by_rule.setdefault(str(rule), RuleTally())
        if status == "pass":
            tally.passed += n
            passed += n
        elif status == "violated":
            tally.violated += n
            violated += n
        else:
            tally.unverifiable += n
            unverifiable += n
    for tally in by_rule.values():
        tally.rate = _rate(tally.passed, tally.violated)
    return AdherenceTally(
        passed=passed,
        violated=violated,
        unverifiable=unverifiable,
        rate=_rate(passed, violated),
        by_rule=dict(sorted(by_rule.items())),
    )


def _tally_violations(adherence: AdherenceTally) -> ViolationTally:
    by_rule = {
        rule: tally.violated for rule, tally in adherence.by_rule.items() if tally.violated
    }
    return ViolationTally(total=adherence.violated, by_rule=by_rule)


def _tally_stated_lab(rows: list[tuple[Any, ...]]) -> StatedLabTally:
    agree = disagree = unverifiable = 0
    for value, count in rows:
        n = int(count)
        if value is True:
            agree += n
        elif value is False:
            disagree += n
        else:
            unverifiable += n
    den = agree + disagree
    return StatedLabTally(
        agree=agree,
        disagree=disagree,
        unverifiable=unverifiable,
        rate=(agree / den) if den else None,
    )


def _session_ids_for(
    con: duckdb.DuckDBPyConnection,
    *,
    weeks: int | None = None,
    week: str | None = None,
    month: str | None = None,
) -> list[str]:
    if week is not None:
        rows = con.execute(
            "SELECT session_id FROM sessions WHERE iso_week = ? ORDER BY session_id",
            [week],
        ).fetchall()
        return [str(row[0]) for row in rows]
    if month is not None:
        rows = con.execute(
            "SELECT session_id FROM sessions WHERE year_month = ? ORDER BY session_id",
            [month],
        ).fetchall()
        return [str(row[0]) for row in rows]
    if weeks is None:
        rows = con.execute("SELECT session_id FROM sessions ORDER BY session_id").fetchall()
        return [str(row[0]) for row in rows]
    latest = con.execute("SELECT max(session_date) FROM sessions").fetchone()
    if not latest or latest[0] is None:
        return []
    latest_day = latest[0]
    if not isinstance(latest_day, date):
        latest_day = date.fromisoformat(str(latest_day)[:10])
    last_week = iso_week_id(latest_day)
    wanted = {shift_iso_week(last_week, delta) for delta in range(1 - weeks, 1)}
    rows = con.execute(
        "SELECT session_id, iso_week FROM sessions ORDER BY session_id"
    ).fetchall()
    return [str(row[0]) for row in rows if row[1] in wanted]


def _summarize_ids(con: duckdb.DuckDBPyConnection, session_ids: list[str]) -> dict[str, Any]:
    if not session_ids:
        return {
            "sessions": 0,
            "trades": 0,
            "hours": 0.0,
            "trades_per_hour": None,
            "adherence": _empty_adherence(),
            "violations": ViolationTally(),
            "stated_vs_lab": StatedLabTally(),
        }
    placeholders = ", ".join("?" for _ in session_ids)
    sess = con.execute(
        f"SELECT COUNT(*), COALESCE(SUM(hours), 0) FROM sessions WHERE session_id IN ({placeholders})",
        session_ids,
    ).fetchone()
    trades = con.execute(
        f"SELECT COUNT(*) FROM trades WHERE session_id IN ({placeholders})",
        session_ids,
    ).fetchone()
    rules = con.execute(
        f"""
        SELECT rule, status, COUNT(*)
        FROM rule_checks
        WHERE session_id IN ({placeholders})
        GROUP BY rule, status
        """,
        session_ids,
    ).fetchall()
    stated = con.execute(
        f"""
        SELECT stated_lab_agree, COUNT(*)
        FROM trades
        WHERE session_id IN ({placeholders})
        GROUP BY stated_lab_agree
        """,
        session_ids,
    ).fetchall()
    n_sessions = int(sess[0]) if sess else 0
    hours = float(sess[1]) if sess else 0.0
    n_trades = int(trades[0]) if trades else 0
    adherence = _tally_rules(rules)
    return {
        "sessions": n_sessions,
        "trades": n_trades,
        "hours": hours,
        "trades_per_hour": _tph(n_trades, hours),
        "adherence": adherence,
        "violations": _tally_violations(adherence),
        "stated_vs_lab": _tally_stated_lab(stated),
    }


def _period(
    con: duckdb.DuckDBPyConnection, kind: str, period_id: str, session_ids: list[str]
) -> LedgerPeriod:
    stats = _summarize_ids(con, session_ids)
    return LedgerPeriod(kind=kind, id=period_id, **stats)  # type: ignore[arg-type]


def _latest_week(con: duckdb.DuckDBPyConnection) -> str | None:
    row = con.execute(
        "SELECT iso_week FROM sessions ORDER BY session_date DESC, session_id DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def _latest_month(con: duckdb.DuckDBPyConnection) -> str | None:
    row = con.execute(
        "SELECT year_month FROM sessions ORDER BY session_date DESC, session_id DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def _pct(rate: float | None) -> str:
    if rate is None:
        return "n/a"
    return f"{rate * 100:.1f}%"


def render_markdown(summary: LedgerSummary) -> str:
    lines = ["# Ledger rollup", ""]
    if summary.weeks is not None:
        lines.append(f"Window: last {summary.weeks} ISO week(s).")
        lines.append("")
    lines.extend(
        [
            f"- Sessions: {summary.sessions}",
            f"- Trades: {summary.trades}",
            f"- Trades/hour: {summary.trades_per_hour if summary.trades_per_hour is not None else 'n/a'}",
            (
                f"- Adherence: {_pct(summary.adherence.rate)} "
                f"({summary.adherence.passed} pass / {summary.adherence.violated} violated)"
            ),
            f"- Violations: {summary.violations.total}",
            (
                f"- Stated vs lab: {_pct(summary.stated_vs_lab.rate)} "
                f"({summary.stated_vs_lab.agree} agree / {summary.stated_vs_lab.disagree} disagree)"
            ),
            "",
        ]
    )
    if not summary.periods:
        lines.append("No sessions in the ledger for this window.")
        lines.append("")
        return "\n".join(lines)
    for period in summary.periods:
        title = "Week" if period.kind == "week" else "Month"
        lines.append(f"## {title} {period.id}")
        lines.append("")
        lines.extend(
            [
                f"- Sessions: {period.sessions}",
                f"- Trades: {period.trades}",
                f"- Trades/hour: {period.trades_per_hour if period.trades_per_hour is not None else 'n/a'}",
                (
                    f"- Adherence: {_pct(period.adherence.rate)} "
                    f"({period.adherence.passed} pass / {period.adherence.violated} violated)"
                ),
                f"- Violations: {period.violations.total}",
                (
                    f"- Stated vs lab: {_pct(period.stated_vs_lab.rate)} "
                    f"({period.stated_vs_lab.agree} agree / {period.stated_vs_lab.disagree} disagree)"
                ),
                "",
            ]
        )
        if period.adherence.by_rule:
            lines.append("| Rule | Pass | Violated | Unverifiable | Adherence |")
            lines.append("| --- | --- | --- | --- | --- |")
            for rule, tally in period.adherence.by_rule.items():
                lines.append(
                    f"| {rule} | {tally.passed} | {tally.violated} | "
                    f"{tally.unverifiable} | {_pct(tally.rate)} |"
                )
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_summary(
    root: Path,
    *,
    weeks: int | None = None,
    week: str | None = None,
    month: str | None = None,
) -> LedgerSummary:
    path = ledger_db_path(root)
    if not path.is_file():
        summary = LedgerSummary(weeks=weeks)
        summary.markdown = render_markdown(summary)
        return summary
    con = _connect(root, read_only=True)
    try:
        periods: list[LedgerPeriod] = []
        if week is not None:
            ids = _session_ids_for(con, week=week)
            stats = _summarize_ids(con, ids)
            periods.append(_period(con, "week", week, ids))
            summary = LedgerSummary(weeks=None, periods=periods, **stats)
        elif month is not None:
            ids = _session_ids_for(con, month=month)
            stats = _summarize_ids(con, ids)
            periods.append(_period(con, "month", month, ids))
            summary = LedgerSummary(weeks=None, periods=periods, **stats)
        else:
            window = 4 if weeks is None else weeks
            if window < 1:
                raise LedgerError("weeks must be >= 1")
            ids = _session_ids_for(con, weeks=window)
            stats = _summarize_ids(con, ids)
            week_ids: list[str] = []
            if ids:
                rows = con.execute(
                    f"""
                    SELECT iso_week, min(session_date)
                    FROM sessions
                    WHERE session_id IN ({", ".join("?" for _ in ids)})
                    GROUP BY iso_week
                    ORDER BY min(session_date)
                    """,
                    ids,
                ).fetchall()
                seen: set[str] = set()
                for row in rows:
                    week_id = str(row[0])
                    if week_id not in seen:
                        seen.add(week_id)
                        week_ids.append(week_id)
            for week_id in week_ids:
                periods.append(
                    _period(con, "week", week_id, _session_ids_for(con, week=week_id))
                )
            summary = LedgerSummary(weeks=window, periods=periods, **stats)
    finally:
        con.close()
    summary.markdown = render_markdown(summary)
    return summary


def rollup(
    root: Path,
    *,
    week: str | None = None,
    month: str | None = None,
) -> RollupResult:
    chosen_week = week
    chosen_month = month
    path = ledger_db_path(root)
    if path.is_file() and (chosen_week == "latest" or chosen_month == "latest"):
        con = _connect(root, read_only=True)
        try:
            if chosen_week == "latest":
                chosen_week = _latest_week(con)
            if chosen_month == "latest":
                chosen_month = _latest_month(con)
        finally:
            con.close()
    if chosen_week == "latest":
        chosen_week = None
    if chosen_month == "latest":
        chosen_month = None
    if chosen_week:
        parse_iso_week(chosen_week)
    if chosen_month:
        parse_year_month(chosen_month)
    if chosen_week and chosen_month:
        week_summary = build_summary(root, week=chosen_week)
        month_summary = build_summary(root, month=chosen_month)
        periods = [*week_summary.periods, *month_summary.periods]
        summary = LedgerSummary(
            weeks=None,
            sessions=week_summary.sessions,
            trades=week_summary.trades,
            hours=week_summary.hours,
            trades_per_hour=week_summary.trades_per_hour,
            adherence=week_summary.adherence,
            violations=week_summary.violations,
            stated_vs_lab=week_summary.stated_vs_lab,
            periods=periods,
        )
        # Combined markdown uses week totals in the header plus both sections.
        header = render_markdown(week_summary)
        month_md = render_markdown(month_summary)
        month_section = month_md.split("## ", 1)
        extra = "## " + month_section[1] if len(month_section) > 1 else ""
        summary.markdown = header.rstrip() + "\n\n" + extra
        if not extra.endswith("\n"):
            summary.markdown += "\n"
        return RollupResult(status="ok", markdown=summary.markdown, summary=summary)
    if chosen_month and not chosen_week:
        summary = build_summary(root, month=chosen_month)
    elif chosen_week:
        summary = build_summary(root, week=chosen_week)
    else:
        summary = build_summary(root, weeks=1)
    return RollupResult(status="ok", markdown=summary.markdown, summary=summary)


def ledger_add(session_id: str, *, root: Path) -> LedgerAddResult:
    return add_session(session_id, root=root)


def ledger_summary(root: Path, *, weeks: int = 4) -> LedgerSummary:
    if weeks < 1:
        raise LedgerError("weeks must be >= 1")
    return build_summary(root, weeks=weeks)
