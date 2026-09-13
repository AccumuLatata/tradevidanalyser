"""Rule scorecard: deterministic fills rules plus evidence-backed speech rules."""

from __future__ import annotations

import math
import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.fills_mirror import JOURNAL_EXCHANGE_TZ, JOURNAL_TICK_SIZE
from tradevidanalyser.schema import (
    Alignment,
    Evidence,
    EvidenceTrade,
    RuleCheck,
    RuleStatus,
    RulesReport,
    SessionEvent,
    SessionRecord,
)

SCHEMA_VERSION = "1"
ENV_RULES = "TVA_RULES"
RULE_IDS = (
    "R-DLL",
    "R-MAX10",
    "R-3L30",
    "R-5M",
    "R-REENTRY",
    "R-CLOSE",
    "R-PLAYBOOK",
    "R-DEFINED",
    "R-3C-CT",
    "R-ARRIVAL",
    "R-SLTP",
    "R-HOURLY",
    "R-ZONE",
    "R-BIAS",
    "R-TILT",
)
EVIDENCE_BACKED_RULE_IDS = RULE_IDS[6:]
STOP_ON_ENTRY_HOLD_S = 60.0
SAME_LEVEL_TICKS = 2
DEFAULT_MAX_TRADES = 10
DEFAULT_STREAK_N = 3
DEFAULT_STREAK_MINUTES = 30
DEFAULT_POST_LOSS_MINUTES = 5
DEFAULT_REENTRY_WINDOW_S = 120.0
DEFAULT_REENTRY_MAX = 1
DEFAULT_REENTRY_BLOCK_MINUTES = 5
# Matches evidence.DEFAULT_PRE_S. Do not import that module (it loads a model adapter).
EVIDENCE_PRE_S = 180.0
TILT_COOLDOWN_S = 300.0
HOURLY_MINUTE_LO = 45
HOURLY_MINUTE_HI = 55
ALIGNMENT_LOW = 0.8
SLTP_REASON = "order modifications not in TradesViz; needs a venue adapter"
STATED_FIELD_NAMES = ("setup", "bias", "stop_raw", "target_raw", "playbook")
_ARRIVAL_RE = re.compile(
    r"arrival|ankunft|kerzenschluss|candle\s*close|close\s+(?:the\s+)?candle",
    re.I,
)
_3C_RE = re.compile(r"\b3c\b", re.I)
_3C_NEGATED_RE = re.compile(r"\b(?:ohne|kein(?:e|en)?|without|no)\s+3c\b", re.I)
_CT_RE = re.compile(r"gegen\s+den\s+trend|counter[- ]?trend|gegen\s+die\s+bewegung", re.I)


@dataclass(frozen=True)
class LossTimeout:
    n: int = DEFAULT_STREAK_N
    minutes: int = DEFAULT_STREAK_MINUTES


@dataclass(frozen=True)
class ReentryConfig:
    window_s: float = DEFAULT_REENTRY_WINDOW_S
    max: int = DEFAULT_REENTRY_MAX
    block_minutes: int = DEFAULT_REENTRY_BLOCK_MINUTES


@dataclass(frozen=True)
class RulesConfig:
    daily_loss_limit_usd: float | None = None
    max_trades_per_day: int = DEFAULT_MAX_TRADES
    consecutive_loss_timeout: LossTimeout = LossTimeout()
    post_loss_block_minutes: int = DEFAULT_POST_LOSS_MINUTES
    reentry: ReentryConfig = ReentryConfig()
    flat_by: time | None = None


@dataclass(frozen=True)
class RuleTrade:
    tva_trade_id: str
    instrument: str
    direction: str
    entry_timestamp: datetime
    exit_timestamp: datetime | None
    entry_price: float | None
    exit_price: float | None
    stop_price: float | None
    hold_seconds: float | None
    net_pnl_currency: float | None
    gross_pnl_currency: float | None
    status: str


@dataclass(frozen=True)
class RulesResult:
    session_id: str
    status: str
    path: str | None = None
    rules: int = 0
    reason: str | None = None

    def as_dict(self) -> dict:
        payload: dict = {
            "status": self.status,
            "session_id": self.session_id,
            "rules": self.rules,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def _r(value: float, ndigits: int = 6) -> float:
    out = round(float(value), ndigits)
    return 0.0 if out == 0.0 else out


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _require_safe_session_id(session_id: str) -> str:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return session_id


def _parse_scalar(raw: str) -> object:
    text = raw.split("#", 1)[0].strip().strip("\"'")
    lowered = text.lower()
    if lowered in {"", "null", "~", "none"}:
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text)
        return float(text)
    except ValueError:
        return text


def parse_rules_yaml(text: str) -> RulesConfig:
    """Parse the checked-in rules.yaml subset (two-level mappings, comments)."""
    data: dict[str, Any] = {}
    section: str | None = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            section = None
            key, _, rest = stripped.partition(":")
            key = key.strip()
            rest = rest.strip()
            if rest:
                data[key] = _parse_scalar(rest)
            else:
                section = key
                data[key] = {}
            continue
        if section is None or not isinstance(data.get(section), dict):
            raise ValueError(f"unreadable rules.yaml line: {raw}")
        key, _, rest = stripped.partition(":")
        data[section][key.strip()] = _parse_scalar(rest)
    return _config_from_mapping(data)


def _as_time(value: object) -> time | None:
    if value is None:
        return None
    if isinstance(value, time):
        return value
    text = str(value).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) < 2:
        raise ValueError(f"unreadable flat_by {value!r}")
    hour = int(parts[0])
    minute = int(parts[1])
    second = int(float(parts[2])) if len(parts) > 2 else 0
    return time(hour, minute, second)


def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be an integer")
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def _require_float(value: object, name: str, *, minimum: float = 0.0) -> float:
    if value is None or isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    number = _finite(value)
    if number is None:
        raise ValueError(f"{name} must be a number")
    if number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def _config_from_mapping(data: dict[str, Any]) -> RulesConfig:
    timeout_raw = data.get("consecutive_loss_timeout") or {}
    if not isinstance(timeout_raw, dict):
        raise ValueError("consecutive_loss_timeout must be a mapping")
    reentry_raw = data.get("reentry") or {}
    if not isinstance(reentry_raw, dict):
        raise ValueError("reentry must be a mapping")
    raw_limit = data.get("daily_loss_limit_usd")
    if raw_limit is None:
        limit = None
    else:
        if isinstance(raw_limit, bool):
            raise ValueError("daily_loss_limit_usd must be a number or null")
        limit = _finite(raw_limit)
        if limit is None:
            raise ValueError("daily_loss_limit_usd must be a number or null")
        if limit < 0:
            raise ValueError("daily_loss_limit_usd must be >= 0")
    return RulesConfig(
        daily_loss_limit_usd=limit,
        max_trades_per_day=_require_int(
            data.get("max_trades_per_day", DEFAULT_MAX_TRADES), "max_trades_per_day"
        ),
        consecutive_loss_timeout=LossTimeout(
            n=_require_int(
                timeout_raw.get("n", DEFAULT_STREAK_N), "consecutive_loss_timeout.n", minimum=1
            ),
            minutes=_require_int(
                timeout_raw.get("minutes", DEFAULT_STREAK_MINUTES),
                "consecutive_loss_timeout.minutes",
            ),
        ),
        post_loss_block_minutes=_require_int(
            data.get("post_loss_block_minutes", DEFAULT_POST_LOSS_MINUTES),
            "post_loss_block_minutes",
        ),
        reentry=ReentryConfig(
            window_s=_require_float(
                reentry_raw.get("window_s", DEFAULT_REENTRY_WINDOW_S), "reentry.window_s"
            ),
            max=_require_int(reentry_raw.get("max", DEFAULT_REENTRY_MAX), "reentry.max"),
            block_minutes=_require_int(
                reentry_raw.get("block_minutes", DEFAULT_REENTRY_BLOCK_MINUTES),
                "reentry.block_minutes",
            ),
        ),
        flat_by=_as_time(data.get("flat_by")),
    )


def default_rules_path() -> Path:
    env = (os.environ.get(ENV_RULES) or "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{ENV_RULES}={env!r} is not a file")
        return path
    here = Path(__file__).resolve().parent
    candidates = [here / "rules.yaml"]
    if len(here.parents) >= 2:
        candidates.append(here.parents[1] / "rules.yaml")
    candidates.append(Path.cwd() / "rules.yaml")
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    raise FileNotFoundError("rules.yaml not found (set TVA_RULES or keep the shipped file)")


def load_rules_config(path: Path | None = None) -> RulesConfig:
    target = path or default_rules_path()
    return parse_rules_yaml(target.read_text(encoding="utf-8"))


def realized_pnl(trade: RuleTrade) -> tuple[float | None, str | None]:
    """Prefer net (fees included); else gross + ``fees_unknown``."""
    net = _finite(trade.net_pnl_currency)
    if net is not None:
        return net, None
    gross = _finite(trade.gross_pnl_currency)
    if gross is not None:
        return gross, "fees_unknown"
    return None, "pnl_unknown"


def _is_closed(trade: RuleTrade) -> bool:
    return trade.exit_timestamp is not None or trade.status == "closed"


def _is_loss(trade: RuleTrade) -> bool:
    pnl, _note = realized_pnl(trade)
    return pnl is not None and pnl < 0


def _is_stop_on_entry(trade: RuleTrade) -> bool:
    if not _is_closed(trade) or not _is_loss(trade):
        return False
    if trade.hold_seconds is not None:
        return trade.hold_seconds <= STOP_ON_ENTRY_HOLD_S
    if trade.stop_price is not None and trade.exit_price is not None:
        return abs(trade.exit_price - trade.stop_price) <= JOURNAL_TICK_SIZE + 1e-9
    return False


def _same_level(a: RuleTrade, b: RuleTrade) -> bool:
    if (a.instrument or "") != (b.instrument or ""):
        return False
    if (a.direction or "") != (b.direction or ""):
        return False
    if a.entry_price is None or b.entry_price is None:
        return False
    return abs(a.entry_price - b.entry_price) <= SAME_LEVEL_TICKS * JOURNAL_TICK_SIZE + 1e-9


def _gap_s(earlier_exit: datetime, later_entry: datetime) -> float:
    return (later_entry - earlier_exit).total_seconds()


def _next_after(
    trades: list[RuleTrade],
    after: datetime,
    *,
    exclude: str | None = None,
) -> RuleTrade | None:
    later = [
        trade
        for trade in trades
        if trade.entry_timestamp >= after and trade.tva_trade_id != exclude
    ]
    return later[0] if later else None


def _next_same_level_after(trades: list[RuleTrade], prev: RuleTrade) -> RuleTrade | None:
    if prev.exit_timestamp is None:
        return None
    for trade in trades:
        if trade.tva_trade_id == prev.tva_trade_id:
            continue
        if trade.entry_timestamp < prev.exit_timestamp:
            continue
        if _same_level(prev, trade):
            return trade
    return None


def _check(rule: str, status: RuleStatus, evidence: dict, reason: str | None = None) -> RuleCheck:
    return RuleCheck(rule=rule, status=status, evidence=evidence, reason=reason)


def _eval_dll(trades: list[RuleTrade], config: RulesConfig) -> RuleCheck:
    notes: list[str] = []
    unknown: list[str] = []
    total = 0.0
    used: list[str] = []
    for trade in trades:
        if not _is_closed(trade):
            continue
        pnl, note = realized_pnl(trade)
        if pnl is None:
            unknown.append(trade.tva_trade_id)
            continue
        total += pnl
        used.append(trade.tva_trade_id)
        if note:
            notes.append(note)
    evidence = {
        "daily_pnl": _r(total),
        "limit_usd": config.daily_loss_limit_usd,
        "trade_ids": used,
        "fees_unknown": "fees_unknown" in notes,
        "unknown_pnl_trades": unknown,
    }
    if config.daily_loss_limit_usd is None:
        return _check(
            "R-DLL",
            "unverifiable",
            evidence,
            "daily_loss_limit_usd is unset (D4)",
        )
    if unknown:
        return _check(
            "R-DLL",
            "unverifiable",
            evidence,
            "closed trade missing net_pnl_currency and gross_pnl_currency",
        )
    limit = float(config.daily_loss_limit_usd)
    if total < -limit:
        return _check("R-DLL", "violated", evidence, f"daily pnl {total:.2f} below -{limit:g}")
    reason = "fees_unknown" if evidence["fees_unknown"] else None
    return _check("R-DLL", "pass", evidence, reason)


def _eval_max10(trades: list[RuleTrade], config: RulesConfig) -> RuleCheck:
    ids = [trade.tva_trade_id for trade in trades]
    evidence = {"count": len(ids), "max": config.max_trades_per_day, "trade_ids": ids}
    if len(ids) > config.max_trades_per_day:
        return _check("R-MAX10", "violated", evidence, f"{len(ids)} trades exceeds {config.max_trades_per_day}")
    return _check("R-MAX10", "pass", evidence)


def _eval_3l30(trades: list[RuleTrade], config: RulesConfig) -> RuleCheck:
    n = config.consecutive_loss_timeout.n
    minutes = config.consecutive_loss_timeout.minutes
    need = minutes * 60
    closed = [trade for trade in trades if _is_closed(trade)]
    streak: list[str] = []
    breaches: list[dict] = []
    for trade in closed:
        pnl, _note = realized_pnl(trade)
        if pnl is None:
            streak = []
            continue
        if pnl < 0:
            streak.append(trade.tva_trade_id)
            if len(streak) >= n and trade.exit_timestamp is not None:
                nxt = _next_after(trades, trade.exit_timestamp, exclude=trade.tva_trade_id)
                if nxt is not None:
                    gap = _gap_s(trade.exit_timestamp, nxt.entry_timestamp)
                    if gap < need:
                        breaches.append(
                            {
                                "after": list(streak[-n:]),
                                "next": nxt.tva_trade_id,
                                "gap_s": _r(gap, 3),
                                "required_s": need,
                            }
                        )
        else:
            streak = []
    evidence = {"n": n, "minutes": minutes, "breaches": breaches}
    if breaches:
        return _check(
            "R-3L30",
            "violated",
            evidence,
            f"entry before {minutes} min timeout after {n} consecutive losses",
        )
    return _check("R-3L30", "pass", evidence)


def _eval_5m(trades: list[RuleTrade], config: RulesConfig) -> RuleCheck:
    minutes = config.post_loss_block_minutes
    need = minutes * 60
    window_s = config.reentry.window_s
    breaches: list[dict] = []
    closed_losses = [
        trade
        for trade in trades
        if _is_closed(trade) and _is_loss(trade) and trade.exit_timestamp is not None
    ]
    for trade in closed_losses:
        nxt = _next_after(trades, trade.exit_timestamp, exclude=trade.tva_trade_id)
        if nxt is None:
            continue
        gap = _gap_s(trade.exit_timestamp, nxt.entry_timestamp)
        exempt = (
            _is_stop_on_entry(trade)
            and _same_level(trade, nxt)
            and gap <= window_s + 1e-9
        )
        if exempt:
            continue
        if gap < need:
            breaches.append(
                {
                    "loss": trade.tva_trade_id,
                    "next": nxt.tva_trade_id,
                    "gap_s": _r(gap, 3),
                    "required_s": need,
                }
            )
    evidence = {"minutes": minutes, "breaches": breaches}
    if breaches:
        return _check("R-5M", "violated", evidence, f"entry before {minutes} min block after a loss")
    return _check("R-5M", "pass", evidence)


def _eval_reentry(trades: list[RuleTrade], config: RulesConfig) -> RuleCheck:
    window_s = config.reentry.window_s
    allowed = config.reentry.max
    block_minutes = config.reentry.block_minutes
    block_s = block_minutes * 60
    breaches: list[dict] = []
    consumed: set[str] = set()
    for seed in trades:
        if seed.tva_trade_id in consumed or not _is_stop_on_entry(seed):
            continue
        cluster = [seed]
        while True:
            prev = cluster[-1]
            if prev.exit_timestamp is None or not _is_loss(prev):
                break
            nxt = _next_same_level_after(trades, prev)
            if nxt is None:
                break
            gap = _gap_s(prev.exit_timestamp, nxt.entry_timestamp)
            if gap > window_s + 1e-9:
                break
            cluster.append(nxt)
        for trade in cluster:
            consumed.add(trade.tva_trade_id)
        extras = len(cluster) - 1
        if extras > allowed:
            breaches.append(
                {
                    "kind": "too_many_reentries",
                    "trade_ids": [trade.tva_trade_id for trade in cluster],
                    "max": allowed,
                }
            )
        last = cluster[-1]
        if extras >= allowed and _is_loss(last) and last.exit_timestamp is not None:
            nxt = _next_same_level_after(trades, last)
            if nxt is not None:
                gap = _gap_s(last.exit_timestamp, nxt.entry_timestamp)
                if gap < block_s:
                    breaches.append(
                        {
                            "kind": "block_after_second_failure",
                            "after": last.tva_trade_id,
                            "next": nxt.tva_trade_id,
                            "gap_s": _r(gap, 3),
                            "required_s": block_s,
                        }
                    )
    evidence = {
        "window_s": window_s,
        "max": allowed,
        "block_minutes": block_minutes,
        "breaches": breaches,
    }
    if breaches:
        return _check(
            "R-REENTRY",
            "violated",
            evidence,
            f"re-entry cluster exceeded max or {block_minutes} min block",
        )
    return _check("R-REENTRY", "pass", evidence)


def _session_day(trades: list[RuleTrade], session_date: date | None) -> date | None:
    if session_date is not None:
        return session_date
    if not trades:
        return None
    last = max(trade.exit_timestamp or trade.entry_timestamp for trade in trades)
    return last.astimezone(JOURNAL_EXCHANGE_TZ).date()


def _eval_close(trades: list[RuleTrade], config: RulesConfig, session_date: date | None) -> RuleCheck:
    cutoff_clock = config.flat_by
    evidence: dict[str, Any] = {
        "flat_by": cutoff_clock.isoformat(timespec="minutes") if cutoff_clock else None,
        "open_trades": [trade.tva_trade_id for trade in trades if not _is_closed(trade)],
    }
    if cutoff_clock is None:
        return _check("R-CLOSE", "unverifiable", evidence, "flat_by is unset")
    day = _session_day(trades, session_date)
    if day is None:
        return _check("R-CLOSE", "pass", evidence)
    cutoff = datetime.combine(day, cutoff_clock, tzinfo=JOURNAL_EXCHANGE_TZ)
    evidence["cutoff"] = cutoff.isoformat()
    late: list[str] = []
    for trade in trades:
        stamp = trade.exit_timestamp or trade.entry_timestamp
        if stamp > cutoff:
            late.append(trade.tva_trade_id)
    evidence["late_trades"] = late
    if evidence["open_trades"]:
        return _check("R-CLOSE", "violated", evidence, "open trade past session; not flat")
    if late:
        return _check("R-CLOSE", "violated", evidence, "exit after flat_by cutoff")
    return _check("R-CLOSE", "pass", evidence)


@dataclass(frozen=True)
class EvidenceCtx:
    evidence: Evidence | None
    events: tuple[SessionEvent, ...]
    session_start: datetime | None
    alignment: Alignment | None
    fills: tuple[RuleTrade, ...] = ()


def _uniq(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _stated_cite(trade: EvidenceTrade, name: str):
    return getattr(trade.stated, name)


def _stated_segs(trade: EvidenceTrade, *names: str) -> list[str]:
    segs: list[str] = []
    for name in names or STATED_FIELD_NAMES:
        cite = _stated_cite(trade, name)
        if cite is not None and cite.seg:
            segs.append(cite.seg)
    return _uniq(segs)


def _trade_has_speech(trade: EvidenceTrade) -> bool:
    if any((seg or "").strip() for seg in trade.commentary):
        return True
    return bool(_stated_segs(trade, *STATED_FIELD_NAMES))


def _session_has_speech(ctx: EvidenceCtx) -> bool:
    if ctx.events:
        return True
    if ctx.evidence is None:
        return False
    return any(_trade_has_speech(trade) for trade in ctx.evidence.trades)


def _spoken_trades(ctx: EvidenceCtx) -> list[EvidenceTrade]:
    if ctx.evidence is None:
        return []
    return [trade for trade in ctx.evidence.trades if _trade_has_speech(trade)]


def _has_3c(text: str) -> bool:
    """True when speech names 3c as a confirmation, not a denial (ohne/kein 3c)."""
    if not text:
        return False
    return bool(_3C_RE.search(_3C_NEGATED_RE.sub(" ", text)))


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _wall_to_video(
    session_start: datetime,
    wall: datetime,
    alignment: Alignment | None,
) -> float:
    """Invert §3.5. Naive stamps are UTC (same convention as ``_as_dt``)."""
    offset = alignment.offset_s if alignment is not None else 0.0
    drift = alignment.drift_s_per_h if alignment is not None else 0.0
    delta = (_aware(wall) - _aware(session_start)).total_seconds()
    denom = 1.0 + float(drift) / 3600.0
    if abs(denom) < 1e-12:
        denom = 1.0
    return (delta - float(offset)) / denom


def _entry_video_t(trade: EvidenceTrade, ctx: EvidenceCtx) -> float:
    """Fill entry in video time. Window pad is a fallback when fills are absent.

    ``window.t0 + PRE_S`` is wrong when the window was clipped at t=0 or the
    pad was not the default 180 s — prefer the fill clock whenever we have it.
    """
    if ctx.session_start is not None:
        for fill in ctx.fills:
            if fill.tva_trade_id == trade.tva_trade_id:
                return _wall_to_video(ctx.session_start, fill.entry_timestamp, ctx.alignment)
    return trade.window.t0 + EVIDENCE_PRE_S


def _entry_points(ctx: EvidenceCtx) -> list[tuple[str, float]]:
    """Video-time entries from fills when the session clock is known, else windows."""
    if ctx.session_start is not None and ctx.fills:
        return [
            (
                fill.tva_trade_id,
                _wall_to_video(ctx.session_start, fill.entry_timestamp, ctx.alignment),
            )
            for fill in ctx.fills
        ]
    if ctx.evidence is None:
        return []
    return [(trade.tva_trade_id, _entry_video_t(trade, ctx)) for trade in ctx.evidence.trades]


def _trade_speech_text(
    trade: EvidenceTrade,
    events: Sequence[SessionEvent],
    *,
    until: float | None = None,
) -> str:
    parts: list[str] = []
    for name in STATED_FIELD_NAMES:
        cite = _stated_cite(trade, name)
        if cite is not None and cite.value:
            parts.append(cite.value)
    t1 = trade.window.t1 if until is None else until
    for event in events:
        if trade.window.t0 <= event.t <= t1 and event.text:
            parts.append(event.text)
    return " ".join(parts)


def _event_segs(
    events: Sequence[SessionEvent],
    *,
    kind: str | None = None,
    pattern: re.Pattern[str] | None = None,
    t0: float | None = None,
    t1: float | None = None,
) -> list[str]:
    segs: list[str] = []
    for event in events:
        if kind is not None and event.kind != kind:
            continue
        if t0 is not None and event.t < t0:
            continue
        if t1 is not None and event.t > t1:
            continue
        if pattern is not None and not pattern.search(event.text or ""):
            continue
        if event.seg:
            segs.append(event.seg)
    return _uniq(segs)


def _alignment_too_low(ctx: EvidenceCtx) -> bool:
    if ctx.alignment is not None:
        return ctx.alignment.confidence < ALIGNMENT_LOW
    if ctx.evidence is not None and ctx.evidence.trades:
        return all(
            trade.alignment == "low" or trade.alignment_confidence < ALIGNMENT_LOW
            for trade in ctx.evidence.trades
        )
    return False


def _video_to_wall(
    session_start: datetime,
    video_t: float,
    alignment: Alignment | None,
) -> datetime:
    offset = alignment.offset_s if alignment is not None else 0.0
    drift = alignment.drift_s_per_h if alignment is not None else 0.0
    delta = video_t + offset + drift * (video_t / 3600.0)
    return session_start + timedelta(seconds=delta)


def _event_minute(event: SessionEvent, ctx: EvidenceCtx) -> int:
    if ctx.session_start is not None:
        return _video_to_wall(ctx.session_start, event.t, ctx.alignment).minute
    return int(event.t % 3600) // 60


def _pass_cited(rule: str, evidence: dict, reason: str | None = None) -> RuleCheck:
    if not evidence.get("segs"):
        return _check(rule, "unverifiable", evidence, reason or "no cited segment")
    return _check(rule, "pass", evidence, reason)


def _absent_speech(rule: str) -> RuleCheck:
    return _check(rule, "unverifiable", {"segs": []}, "absent speech")


def _eval_playbook(ctx: EvidenceCtx) -> RuleCheck:
    spoken = _spoken_trades(ctx)
    if not spoken:
        return _absent_speech("R-PLAYBOOK")
    violated: list[str] = []
    segs: list[str] = []
    for trade in spoken:
        cited = _stated_segs(trade, "playbook", "setup")
        if cited:
            segs.extend(cited)
        else:
            violated.append(trade.tva_trade_id)
    evidence = {"segs": _uniq(segs), "trade_ids": violated}
    if violated:
        return _check(
            "R-PLAYBOOK",
            "violated",
            evidence,
            "playbook not spoken before entry",
        )
    return _pass_cited("R-PLAYBOOK", evidence)


def _eval_defined(ctx: EvidenceCtx) -> RuleCheck:
    spoken = _spoken_trades(ctx)
    if not spoken:
        return _absent_speech("R-DEFINED")
    violated: list[str] = []
    segs: list[str] = []
    for trade in spoken:
        cited = _stated_segs(trade, "stop_raw", "target_raw")
        stop = _stated_cite(trade, "stop_raw")
        target = _stated_cite(trade, "target_raw")
        if stop is not None and stop.seg and target is not None and target.seg:
            segs.extend(cited)
        else:
            violated.append(trade.tva_trade_id)
            segs.extend(cited)
    evidence = {"segs": _uniq(segs), "trade_ids": violated}
    if violated:
        return _check(
            "R-DEFINED",
            "violated",
            evidence,
            "stop or target missing from pre-entry window",
        )
    return _pass_cited("R-DEFINED", evidence)


def _cited_matching(
    trade: EvidenceTrade,
    ctx: EvidenceCtx,
    predicate: Callable[[str], object],
    *,
    until: float,
) -> list[str]:
    segs: list[str] = []
    for event in ctx.events:
        if not (trade.window.t0 <= event.t <= until):
            continue
        if event.seg and predicate(event.text or ""):
            segs.append(event.seg)
    for name in STATED_FIELD_NAMES:
        cite = _stated_cite(trade, name)
        if cite is not None and cite.seg and predicate(cite.value or ""):
            segs.append(cite.seg)
    return _uniq(segs)


def _eval_3c_ct(ctx: EvidenceCtx) -> RuleCheck:
    spoken = _spoken_trades(ctx)
    if not spoken:
        return _absent_speech("R-3C-CT")
    violated: list[str] = []
    passed: list[str] = []
    segs: list[str] = []
    for trade in spoken:
        entry_t = _entry_video_t(trade, ctx)
        text = _trade_speech_text(trade, ctx.events, until=entry_t)
        cited = _cited_matching(trade, ctx, _has_3c, until=entry_t)
        if _has_3c(text):
            if cited:
                segs.extend(cited)
                passed.append(trade.tva_trade_id)
            continue
        if _CT_RE.search(text):
            violated.append(trade.tva_trade_id)
            segs.extend(_cited_matching(trade, ctx, _CT_RE.search, until=entry_t))
    evidence = {"segs": _uniq(segs), "trade_ids": violated, "passed": passed}
    if violated:
        return _check(
            "R-3C-CT",
            "violated",
            evidence,
            "counter-trend speech without 3c",
        )
    if passed:
        return _pass_cited("R-3C-CT", evidence)
    return _check(
        "R-3C-CT",
        "unverifiable",
        evidence,
        "no 3c or counter-trend speech; journal triggers later",
    )


def _eval_arrival(ctx: EvidenceCtx) -> RuleCheck:
    spoken = _spoken_trades(ctx)
    if not spoken:
        return _absent_speech("R-ARRIVAL")
    violated: list[str] = []
    passed: list[str] = []
    segs: list[str] = []
    for trade in spoken:
        entry_t = _entry_video_t(trade, ctx)
        text = _trade_speech_text(trade, ctx.events, until=entry_t)
        cited = _cited_matching(trade, ctx, _ARRIVAL_RE.search, until=entry_t)
        if _ARRIVAL_RE.search(text):
            if cited:
                segs.extend(cited)
                passed.append(trade.tva_trade_id)
            continue
        violated.append(trade.tva_trade_id)
    evidence = {"segs": _uniq(segs), "trade_ids": violated, "passed": passed}
    if violated:
        return _check(
            "R-ARRIVAL",
            "violated",
            evidence,
            "no arrival-candle close cue",
        )
    return _pass_cited("R-ARRIVAL", evidence)


def _eval_sltp(_ctx: EvidenceCtx) -> RuleCheck:
    return _check("R-SLTP", "unverifiable", {"segs": []}, SLTP_REASON)


def _eval_hourly(ctx: EvidenceCtx) -> RuleCheck:
    if not _session_has_speech(ctx):
        return _absent_speech("R-HOURLY")
    if _alignment_too_low(ctx):
        return _check(
            "R-HOURLY",
            "unverifiable",
            {"segs": []},
            "alignment confidence below threshold",
        )
    checkins = [event for event in ctx.events if event.kind == "hourly_checkin"]
    if not checkins:
        return _check(
            "R-HOURLY",
            "violated",
            {"segs": []},
            "no hourly check-in language",
        )
    near: list[str] = []
    far: list[str] = []
    far_hit = False
    for event in checkins:
        is_near = HOURLY_MINUTE_LO <= _event_minute(event, ctx) <= HOURLY_MINUTE_HI
        if not is_near:
            far_hit = True
        if event.seg:
            (near if is_near else far).append(event.seg)
    evidence = {"segs": _uniq([*near, *far]), "near": near, "far": far}
    if far_hit:
        return _check("R-HOURLY", "violated", evidence, "check-in not near xx:50")
    return _pass_cited("R-HOURLY", evidence)


def _eval_zone(ctx: EvidenceCtx) -> RuleCheck:
    if not _session_has_speech(ctx):
        return _absent_speech("R-ZONE")
    if _alignment_too_low(ctx):
        return _check(
            "R-ZONE",
            "unverifiable",
            {"segs": []},
            "alignment confidence below threshold",
        )
    zones = [event for event in ctx.events if event.kind == "no_trade_zone"]
    if not zones:
        return _check(
            "R-ZONE",
            "unverifiable",
            {"segs": []},
            "no no-trade zone declared",
        )
    overlaps: list[dict] = []
    for event in zones:
        for trade_id, entry_t in _entry_points(ctx):
            # Pre-entry lookback only. The evidence window includes post-exit
            # speech; a "keine Trades" after flatten is not an entry during a zone.
            if entry_t - EVIDENCE_PRE_S - 1e-9 <= event.t <= entry_t + 1e-9:
                overlaps.append({"tva_trade_id": trade_id, "seg": event.seg, "t": event.t})
    segs = _uniq(event.seg for event in zones if event.seg)
    evidence = {"segs": segs, "overlaps": overlaps}
    if overlaps:
        return _check("R-ZONE", "violated", evidence, "entry during a no-trade zone")
    return _pass_cited("R-ZONE", evidence)


def _eval_bias(ctx: EvidenceCtx) -> RuleCheck:
    if not _session_has_speech(ctx):
        return _absent_speech("R-BIAS")
    segs = _event_segs(ctx.events, kind="bias_statement")
    if ctx.evidence is not None:
        for trade in ctx.evidence.trades:
            segs.extend(_stated_segs(trade, "bias"))
    segs = _uniq(segs)
    evidence = {"segs": segs}
    if segs:
        return _pass_cited("R-BIAS", evidence)
    return _check("R-BIAS", "violated", evidence, "bias not re-evaluated")


def _eval_tilt(ctx: EvidenceCtx) -> RuleCheck:
    tilts = [event for event in ctx.events if event.kind == "tilt"]
    if not tilts:
        return _check("R-TILT", "unverifiable", {"segs": []}, "no tilt language")
    if _alignment_too_low(ctx):
        return _check(
            "R-TILT",
            "unverifiable",
            {"segs": _uniq(event.seg for event in tilts if event.seg)},
            "alignment confidence below threshold",
        )
    breaches: list[dict] = []
    for event in tilts:
        for trade_id, entry_t in _entry_points(ctx):
            if event.t < entry_t <= event.t + TILT_COOLDOWN_S:
                breaches.append(
                    {
                        "tva_trade_id": trade_id,
                        "seg": event.seg,
                        "gap_s": _r(entry_t - event.t, 3),
                    }
                )
    segs = _uniq(event.seg for event in tilts if event.seg)
    evidence = {"segs": segs, "breaches": breaches, "cooldown_s": TILT_COOLDOWN_S}
    if breaches:
        return _check("R-TILT", "violated", evidence, "entry during tilt cool-down")
    return _pass_cited("R-TILT", evidence)


def evaluate_rules(
    trades: list[RuleTrade],
    config: RulesConfig | None = None,
    *,
    session_date: date | None = None,
    evidence: Evidence | None = None,
    session_events: Sequence[SessionEvent] | None = None,
    session_start: datetime | None = None,
    alignment: Alignment | None = None,
) -> list[RuleCheck]:
    """Score the catalog. Deterministic rows use fills only; evidence-backed
    rows read already-written evidence.json and session_events. Never calls a model.
    """
    cfg = config or RulesConfig()
    ordered = sorted(trades, key=lambda trade: (trade.entry_timestamp, trade.tva_trade_id))
    ctx = EvidenceCtx(
        evidence=evidence,
        events=tuple(session_events or ()),
        session_start=session_start,
        alignment=alignment,
        fills=tuple(ordered),
    )
    return [
        _eval_dll(ordered, cfg),
        _eval_max10(ordered, cfg),
        _eval_3l30(ordered, cfg),
        _eval_5m(ordered, cfg),
        _eval_reentry(ordered, cfg),
        _eval_close(ordered, cfg, session_date),
        _eval_playbook(ctx),
        _eval_defined(ctx),
        _eval_3c_ct(ctx),
        _eval_arrival(ctx),
        _eval_sltp(ctx),
        _eval_hourly(ctx),
        _eval_zone(ctx),
        _eval_bias(ctx),
        _eval_tilt(ctx),
    ]


def _as_dt(value: object) -> datetime | None:
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
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _record_session_date(record: SessionRecord) -> date | None:
    parsed = _as_dt(record.recording.start_wallclock_vienna)
    if parsed is None:
        return None
    return parsed.astimezone(JOURNAL_EXCHANGE_TZ).date()


def _as_str(value: object) -> str:
    return "" if value is None else str(value)


def read_rule_trades(path: Path) -> list[RuleTrade]:
    table = pq.read_table(path)
    names = set(table.column_names)
    n = table.num_rows

    def col(name: str) -> list:
        if name not in names:
            return [None] * n
        return table.column(name).to_pylist()

    ids = col("tva_trade_id")
    fallback = col("trade_id")
    entries = col("entry_timestamp")
    exits = col("exit_timestamp")
    instruments = col("instrument")
    directions = col("direction")
    entry_prices = col("entry_price")
    exit_prices = col("exit_price")
    stop_prices = col("stop_price")
    holds = col("hold_seconds")
    nets = col("net_pnl_currency")
    grosses = col("gross_pnl_currency")
    statuses = col("status")
    rows: list[RuleTrade] = []
    for index in range(n):
        entry = _as_dt(entries[index])
        if entry is None:
            continue
        tva_id = _as_str(ids[index]) or _as_str(fallback[index]) or f"T{index + 1:02d}"
        exit_ts = _as_dt(exits[index])
        rows.append(
            RuleTrade(
                tva_trade_id=tva_id,
                instrument=_as_str(instruments[index]),
                direction=_as_str(directions[index]),
                entry_timestamp=entry,
                exit_timestamp=exit_ts,
                entry_price=_finite(entry_prices[index]),
                exit_price=_finite(exit_prices[index]),
                stop_price=_finite(stop_prices[index]),
                hold_seconds=_finite(holds[index]),
                net_pnl_currency=_finite(nets[index]),
                gross_pnl_currency=_finite(grosses[index]),
                status=_as_str(statuses[index]) or ("closed" if exit_ts is not None else "open"),
            )
        )
    rows.sort(key=lambda trade: (trade.entry_timestamp, trade.tva_trade_id))
    return rows


def build_rules_report(
    record: SessionRecord,
    *,
    root: Path,
    config: RulesConfig | None = None,
    config_path: Path | None = None,
) -> RulesReport | None:
    session_id = _require_safe_session_id(record.id)
    trades_file = store.trades_path(root, session_id)
    if not trades_file.is_file():
        return None
    trades = read_rule_trades(trades_file)
    cfg = config or load_rules_config(config_path)
    evidence = _load_evidence(root, session_id)
    events = _load_session_events(root, session_id)
    return RulesReport(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        rules=evaluate_rules(
            trades,
            cfg,
            session_date=_record_session_date(record),
            evidence=evidence,
            session_events=events,
            session_start=_as_dt(record.recording.start_wallclock_vienna),
            alignment=record.alignment,
        ),
    )


def _load_evidence(root: Path, session_id: str) -> Evidence | None:
    path = store.evidence_path(root, session_id)
    if not path.is_file():
        return None
    try:
        return Evidence.model_validate(store.read_json(path))
    except (OSError, TypeError, ValueError):
        # Fail closed: do not block deterministic rows on a bad evidence.json.
        return None


def _load_session_events(root: Path, session_id: str) -> list[SessionEvent]:
    path = store.insights_path(root, session_id)
    if not path.is_file():
        return []
    try:
        return list(store.load_insights(root, session_id).session_events)
    except (OSError, TypeError, ValueError):
        return []


def rules_session(
    session_id: str,
    *,
    root: Path,
    config: RulesConfig | None = None,
    config_path: Path | None = None,
) -> RulesResult:
    session_id = _require_safe_session_id(session_id)
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    report = build_rules_report(record, root=root, config=config, config_path=config_path)
    if report is None:
        path = store.rules_path(root, session_id)
        path.unlink(missing_ok=True)
        store.compute_status(root, session_id)
        return RulesResult(session_id=session_id, status="skipped", reason="no trades.parquet")
    store.write_json(store.rules_path(root, session_id), report.model_dump(mode="json"))
    store.compute_status(root, session_id)
    return RulesResult(
        session_id=session_id,
        status="ok",
        path="rules.json",
        rules=len(report.rules),
    )
