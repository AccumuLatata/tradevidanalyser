"""Session debrief from files + a cited ReportProvider (PR-23)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import httpx
import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.providers.extract import default_prompt_path, prompt_version_for
from tradevidanalyser.schema import (
    DebriefReport,
    DebriefSection,
    SessionContext,
    SessionRecord,
)

SCHEMA_VERSION = "1"
ENV_REPORT = "TVA_REPORT_PROVIDER"
ENV_REPORT_MODEL = "TVA_REPORT_MODEL"
ENV_XAI_KEY = "XAI_API_KEY"
PROMPT_FILENAME = "debrief_v1.md"
PROMPT_VERSION_FAKE = "debrief-fake-v1"
DEFAULT_GROK_MODEL = "grok-4.6"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
HTTP_TIMEOUT = 3600.0
DIGIT_RUN = re.compile(r"\d+")
# Structured ids that must already be on the allow-list if they appear in prose.
ID_IN_TEXT = re.compile(r"\b(?:T\d+|seg_\d+|R-[A-Z0-9]+(?:-[A-Z0-9]+)*)\b|clips/\S+")
OCR_DIGIT_COLUMNS = ("text", "parsed")
CONTEXT_SKIP_KEYS = frozenset({"schema_version"})
SECTION_SPECS = (
    ("source", "Source", "facts"),
    ("day", "Day", "prose"),
    ("trades", "Trades", "facts"),
    ("rules", "Rules", "facts"),
    ("brief_vs_behaviour", "Brief vs behaviour", "prose"),
    ("observations", "Observations", "prose"),
    ("learnings", "Learnings", "prose"),
    ("gaps", "Gaps", "facts"),
)
SECTION_HEADINGS = tuple(f"## {title}" for _sid, title, _kind in SECTION_SPECS)
RULE_LABELS = {
    "R-DLL": "Daily loss limit",
    "R-MAX10": "Max trades",
    "R-3L30": "Consecutive-loss timeout",
    "R-5M": "Post-loss block",
    "R-REENTRY": "Re-entry",
    "R-CLOSE": "Flat-by",
    "R-PLAYBOOK": "Playbook",
    "R-DEFINED": "Defined stop/target",
    "R-3C-CT": "Counter-trend",
    "R-ARRIVAL": "Arrival close",
    "R-SLTP": "Stop/target moves",
    "R-HOURLY": "Hourly check-in",
    "R-ZONE": "Trade zone",
    "R-BIAS": "Bias",
    "R-TILT": "Tilt",
}


class ReportError(ValueError):
    """Unreadable provider output or missing prompt."""


@dataclass(frozen=True)
class ProseSpan:
    text: str
    cites: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ReportProse:
    day: ProseSpan
    brief_vs_behaviour: list[ProseSpan]
    observations: list[ProseSpan]
    learnings: list[ProseSpan]


@dataclass(frozen=True)
class ReportFacts:
    session_id: str
    language: str
    trade_ids: list[str]
    trades: list[dict[str, str]]
    rules: list[dict[str, str]]
    brief: dict[str, str]
    lab: dict[str, dict[str, str]]
    context_gaps: list[str]
    allowed_ids: list[str]
    allowed_runs: set[str]


@dataclass(frozen=True)
class ReportResult:
    session_id: str
    status: str
    path: str | None = None
    json_path: str | None = None
    provider: str | None = None
    gaps: int = 0
    reason: str | None = None

    def as_dict(self) -> dict:
        payload: dict = {
            "status": self.status,
            "session_id": self.session_id,
            "gaps": self.gaps,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.json_path is not None:
            payload["json_path"] = self.json_path
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class ReportProvider(Protocol):
    name: str
    model: str
    prompt_version: str

    def prose(self, facts: ReportFacts) -> ReportProse: ...


def _require_safe_session_id(session_id: str) -> str:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return session_id


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    try:
        if value != value:  # NaN
            return True
    except TypeError:
        pass
    return False


def _datetime_texts(value: datetime) -> list[str]:
    texts: list[str] = []
    try:
        texts.append(value.isoformat())
    except (TypeError, ValueError):
        pass
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    try:
        texts.append(aware.astimezone(timezone.utc).isoformat())
        texts.append(aware.astimezone(VIENNA).isoformat())
    except (TypeError, ValueError):
        pass
    return texts


def _cell_text(value: object) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        try:
            return aware.astimezone(VIENNA).isoformat()
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, date):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _harvest_value(value: object, into: set[str], *, key: str | None = None) -> None:
    if key in CONTEXT_SKIP_KEYS:
        return
    if _is_missing(value) or isinstance(value, bool):
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _harvest_value(item, into)
        return
    if isinstance(value, dict):
        for child_key, item in value.items():
            if child_key in CONTEXT_SKIP_KEYS:
                continue
            # Lab per_trade keys are tva_trade_ids (facts). Field names are not.
            if key == "per_trade" and isinstance(child_key, str):
                _harvest_value(child_key, into)
            _harvest_value(item, into, key=str(child_key))
        return
    if isinstance(value, datetime):
        for text in _datetime_texts(value):
            into.update(DIGIT_RUN.findall(text))
        return
    into.update(DIGIT_RUN.findall(_cell_text(value)))


def _read_parquet_cells(path: Path, columns: tuple[str, ...] | None = None) -> list[object]:
    table = pq.read_table(path)
    names = list(table.column_names)
    if columns is not None:
        names = [name for name in columns if name in names]
    cells: list[object] = []
    for name in names:
        cells.extend(table.column(name).to_pylist())
    return cells


def collect_allowed_runs(root: Path, session_id: str) -> set[str]:
    """Digit runs from trades.parquet, ocr.parquet, and context.json only."""
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    allowed: set[str] = set()
    trades = store.trades_path(root, session_id)
    if trades.is_file():
        for cell in _read_parquet_cells(trades):
            _harvest_value(cell, allowed)
    ocr = store.ocr_path(root, session_id)
    if ocr.is_file():
        for cell in _read_parquet_cells(ocr, columns=OCR_DIGIT_COLUMNS):
            _harvest_value(cell, allowed)
    context = store.context_path(root, session_id)
    if context.is_file():
        raw = context.read_text(encoding="utf-8")
        try:
            data = json.loads(raw)
        except ValueError:
            data = None
        if isinstance(data, dict):
            _harvest_value(data, allowed)
        else:
            allowed.update(DIGIT_RUN.findall(raw))
    return allowed


def digit_audit(text: str, allowed: set[str]) -> list[str]:
    return [run for run in DIGIT_RUN.findall(text) if run not in allowed]


def _safe_text(text: str, allowed: set[str]) -> str:
    if not digit_audit(text, allowed):
        return text
    return ""


def _load_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = store.read_json(path)
    return data if isinstance(data, dict) else {}


def _trade_rows(root: Path, session_id: str) -> list[dict[str, str]]:
    path = store.trades_path(root, session_id)
    if not path.is_file():
        return []
    table = pq.read_table(path)
    names = set(table.column_names)
    n = table.num_rows

    def col(name: str) -> list:
        if name not in names:
            return [None] * n
        return table.column(name).to_pylist()

    ids = col("tva_trade_id")
    fallback = col("trade_id")
    rows: list[dict[str, str]] = []
    for index in range(n):
        tva_id = _cell_text(ids[index]) or _cell_text(fallback[index])
        net = col("net_pnl_currency")[index]
        gross = col("gross_pnl_currency")[index]
        pnl = net if not _is_missing(net) else gross
        rows.append(
            {
                "tva_trade_id": tva_id,
                "direction": _cell_text(col("direction")[index]),
                "instrument": _cell_text(col("instrument")[index]),
                "entry": _cell_text(col("entry_price")[index]),
                "exit": _cell_text(col("exit_price")[index]),
                "pnl": _cell_text(pnl),
            }
        )
    return rows


def _evidence_clips(root: Path, session_id: str) -> dict[str, str]:
    data = _load_json(store.evidence_path(root, session_id))
    clips: dict[str, str] = {}
    for trade in data.get("trades") or []:
        if not isinstance(trade, dict):
            continue
        tva_id = str(trade.get("tva_trade_id") or "")
        clip = trade.get("clip")
        if tva_id and clip:
            clips[tva_id] = str(clip)
    return clips


def _rule_rows(root: Path, session_id: str) -> list[dict[str, str]]:
    data = _load_json(store.rules_path(root, session_id))
    rows: list[dict[str, str]] = []
    for item in data.get("rules") or []:
        if not isinstance(item, dict):
            continue
        rule = str(item.get("rule") or "")
        status = str(item.get("status") or "")
        if rule:
            rows.append({"rule": rule, "status": status, "label": RULE_LABELS.get(rule, rule)})
    return rows


def _allowed_ids(
    record: SessionRecord,
    trades: list[dict[str, str]],
    rules: list[dict[str, str]],
    context: SessionContext | None,
    clips: dict[str, str],
    root: Path,
) -> list[str]:
    ids = [trade["tva_trade_id"] for trade in trades if trade["tva_trade_id"]]
    ids.extend(row["rule"] for row in rules)
    if context is not None:
        if context.brief is not None:
            ids.append("brief")
        if context.drc is not None:
            ids.append("drc")
        if context.lab is not None:
            ids.append("lab")
        ids.append(context.session_id)
    ids.append(record.id)
    if store.insights_path(root, record.id).is_file():
        try:
            insights = store.load_insights(root, record.id)
        except (OSError, TypeError, ValueError):
            insights = None
        if insights is not None:
            for event in insights.session_events:
                if event.seg:
                    ids.append(event.seg)
            for field in (
                insights.bias_statements,
                insights.playbooks_mentioned,
                insights.stated_levels,
                insights.stated_stops_targets,
                insights.checkins,
                insights.tilt_markers,
                insights.brief_refs,
                insights.observations,
            ):
                for span in field:
                    if span.seg:
                        ids.append(span.seg)
    ids.extend(clips.values())
    seen: set[str] = set()
    out: list[str] = []
    for item in ids:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _load_context(root: Path, session_id: str) -> SessionContext | None:
    path = store.context_path(root, session_id)
    if not path.is_file():
        return None
    return SessionContext.model_validate(store.read_json(path))


def gather_facts(record: SessionRecord, *, root: Path) -> ReportFacts:
    session_id = record.id
    trades = _trade_rows(root, session_id)
    rules = _rule_rows(root, session_id)
    context = _load_context(root, session_id)
    clips = _evidence_clips(root, session_id)
    brief: dict[str, str] = {}
    lab: dict[str, dict[str, str]] = {}
    context_gaps: list[str] = []
    if context is not None and context.brief is not None:
        brief = {
            "bias_nq": context.brief.bias_nq or "",
            "bias_es": context.brief.bias_es or "",
            "conviction": context.brief.conviction or "",
            "kill_levels": context.brief.kill_levels or "",
        }
    if context is not None and context.lab is not None:
        for tva_id, row in context.lab.per_trade.items():
            lab[tva_id] = {
                "nearest_level_token": row.nearest_level_token or "",
                "level_context": row.level_context or "",
                "tag_alignment": row.tag_alignment or "",
                "zone_id": row.zone_id or "",
                "inferred_triggers_1m": row.inferred_triggers_1m or "",
            }
    if context is not None:
        context_gaps = list(context.gaps)
    return ReportFacts(
        session_id=session_id,
        language=record.language,
        trade_ids=[row["tva_trade_id"] for row in trades if row["tva_trade_id"]],
        trades=trades,
        rules=rules,
        brief=brief,
        lab=lab,
        context_gaps=context_gaps,
        allowed_ids=_allowed_ids(record, trades, rules, context, clips, root),
        allowed_runs=collect_allowed_runs(root, session_id),
    )


def _keep_span(span: ProseSpan, facts: ReportFacts, gaps: list[str], label: str) -> ProseSpan | None:
    text = (span.text or "").strip()
    cites = [str(cite).strip() for cite in span.cites if str(cite).strip()]
    if not text:
        return None
    if not cites:
        gaps.append(f"dropped uncited {label}")
        return None
    if any(cite not in facts.allowed_ids for cite in cites):
        gaps.append(f"dropped {label} with unknown cite")
        return None
    invented = [token for token in ID_IN_TEXT.findall(text) if token not in facts.allowed_ids]
    if invented:
        gaps.append(f"dropped {label} with invented id")
        return None
    if digit_audit(text, facts.allowed_runs):
        gaps.append(f"dropped {label} with digit not in trades/ocr/context")
        return None
    return ProseSpan(text=text, cites=cites)


def _fallback_learnings(facts: ReportFacts) -> list[ProseSpan]:
    cite = facts.trade_ids[0] if facts.trade_ids else facts.session_id
    if cite not in facts.allowed_ids:
        return []
    lines = [
        f"Name the playbook before entry ({cite})." if cite in facts.trade_ids else "Name the playbook before entry.",
        f"State stop and target before entry ({cite})." if cite in facts.trade_ids else "State stop and target before entry.",
        f"Cool down after tilt language ({cite})." if cite in facts.trade_ids else "Cool down after tilt language.",
    ]
    return [ProseSpan(text=line, cites=[cite]) for line in lines if not digit_audit(line, facts.allowed_runs)]


class FakeReportProvider:
    name = "fake"
    model = "none"
    prompt_version = PROMPT_VERSION_FAKE

    def prose(self, facts: ReportFacts) -> ReportProse:
        cite = facts.trade_ids[0] if facts.trade_ids else ""
        if cite:
            day = ProseSpan(text=f"Reviewed the tape against {cite}.", cites=[cite])
            notes = ProseSpan(text=f"Notes recorded for {cite}.", cites=[cite])
        else:
            session_cite = facts.session_id if facts.session_id in facts.allowed_ids else ""
            day = ProseSpan(
                text="Reviewed available files.",
                cites=[session_cite] if session_cite else [],
            )
            notes = ProseSpan(text="", cites=[])
        brief_bits: list[str] = []
        brief_cites: list[str] = []
        if facts.brief.get("bias_nq") and "brief" in facts.allowed_ids:
            brief_bits.append(f"Brief NQ bias: {facts.brief['bias_nq']}.")
            brief_cites.append("brief")
        if cite and facts.lab.get(cite, {}).get("nearest_level_token") and "lab" in facts.allowed_ids:
            token = facts.lab[cite]["nearest_level_token"]
            brief_bits.append(f"{cite} lab token {token}.")
            brief_cites.append(cite)
            brief_cites.append("lab")
        brief_text = " ".join(brief_bits)
        return ReportProse(
            day=day,
            brief_vs_behaviour=[ProseSpan(text=brief_text, cites=brief_cites)] if brief_text else [],
            observations=[notes] if notes.text else [],
            learnings=_fallback_learnings(facts),
        )


class GrokReportProvider:
    name = "grok"
    model = DEFAULT_GROK_MODEL

    def __init__(self, client: httpx.Client | None = None, *, prompt_path: Path | None = None) -> None:
        self._client = client
        self.prompt_path = prompt_path
        env_model = (os.environ.get(ENV_REPORT_MODEL) or "").strip()
        if env_model:
            self.model = env_model
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        self.prompt_version = prompt_version_for(path)

    def prose(self, facts: ReportFacts) -> ReportProse:
        key = (os.environ.get(ENV_XAI_KEY) or "").strip()
        if not key:
            raise ReportError("XAI_API_KEY is unset. Set it or use TVA_REPORT_PROVIDER=fake.")
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        draft = self._complete(path.read_text(encoding="utf-8"), facts, key)
        day_raw = draft.get("day") if isinstance(draft.get("day"), dict) else {}
        return ReportProse(
            day=ProseSpan(
                text=str(day_raw.get("text") or ""),
                cites=[str(cite) for cite in (day_raw.get("cites") or []) if cite],
            ),
            brief_vs_behaviour=_spans_from(draft.get("brief_vs_behaviour")),
            observations=_spans_from(draft.get("observations")),
            learnings=_spans_from(draft.get("learnings")),
        )

    def _complete(self, system_prompt: str, facts: ReportFacts, key: str) -> dict[str, Any]:
        user = {
            "session_id": facts.session_id,
            "trade_ids": facts.trade_ids,
            "brief": facts.brief,
            "lab": facts.lab,
            "allowed_ids": facts.allowed_ids,
            "allowed_digit_runs": sorted(facts.allowed_runs),
        }
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "debrief_prose",
                    "strict": True,
                    "schema": _prose_schema(),
                },
            },
        }
        own = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            response = client.post(
                XAI_CHAT_URL,
                json=body,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise ReportError(f"Grok request failed: {exc}") from exc
        finally:
            if own:
                client.close()
        if response.status_code >= 400:
            raise ReportError(f"Grok HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReportError("Grok returned non-JSON") from exc
        return _payload_body(payload)


def _spans_from(raw: object) -> list[ProseSpan]:
    if not isinstance(raw, list):
        return []
    out: list[ProseSpan] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        cites = [str(cite) for cite in (item.get("cites") or []) if cite]
        out.append(ProseSpan(text=str(item.get("text") or ""), cites=cites))
    return out


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _payload_body(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReportError("Grok returned a non-object JSON body")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ReportError("Grok response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ReportError("Grok choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise ReportError("Grok choice has no message")
    content = message.get("content")
    if isinstance(content, dict):
        return content
    raw = _message_content(message).strip()
    if not raw:
        refusal = message.get("refusal")
        if refusal:
            raise ReportError(f"Grok refused: {refusal}")
        raise ReportError("Grok message content is empty")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise ReportError("Grok JSON content is not an object") from exc
    if not isinstance(body, dict):
        raise ReportError("Grok JSON content is not an object")
    return body


def _prose_schema() -> dict[str, Any]:
    span = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["text", "cites"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "day": span,
            "brief_vs_behaviour": {"type": "array", "items": span},
            "observations": {"type": "array", "items": span},
            "learnings": {"type": "array", "items": span},
        },
        "required": ["day", "brief_vs_behaviour", "observations", "learnings"],
    }


def get_report_provider(
    name: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> ReportProvider:
    chosen = (name or os.environ.get(ENV_REPORT) or "fake").strip().lower()
    if chosen in {"fake", "test"}:
        return FakeReportProvider()
    if chosen in {"grok", "xai"}:
        return GrokReportProvider(client=client)
    raise ReportError(f"unknown report provider {chosen!r}")


def _join_spans(spans: list[ProseSpan]) -> tuple[str, list[str]]:
    lines = [span.text for span in spans if span.text]
    cites: list[str] = []
    seen: set[str] = set()
    for span in spans:
        for cite in span.cites:
            if cite not in seen:
                seen.add(cite)
                cites.append(cite)
    return "\n".join(lines), cites


def _source_body(facts: ReportFacts, record: SessionRecord) -> str:
    lines: list[str] = []
    session_line = f"Session {facts.session_id}."
    if not digit_audit(session_line, facts.allowed_runs):
        lines.append(session_line)
    lang = f"Language {record.language}."
    if not digit_audit(lang, facts.allowed_runs):
        lines.append(lang)
    if facts.trades:
        lines.append("Trades file present.")
    else:
        lines.append("No trades file.")
    return "\n".join(lines)


def _trades_body(facts: ReportFacts, clips: dict[str, str]) -> str:
    if not facts.trades:
        return "No trades."
    header = "| Trade | Side | Instrument | Entry | Exit | PnL | Clip |"
    sep = "| --- | --- | --- | --- | --- | --- | --- |"
    lines = [header, sep]
    for row in facts.trades:
        clip = "clip" if row["tva_trade_id"] in clips else ""
        line = (
            f"| {row['tva_trade_id']} | {row['direction']} | {row['instrument']} | "
            f"{row['entry']} | {row['exit']} | {row['pnl']} | {clip} |"
        )
        if digit_audit(line, facts.allowed_runs):
            continue
        lines.append(line)
    return "\n".join(lines)


def _rules_body(facts: ReportFacts) -> str:
    if not facts.rules:
        return "No rules file."
    lines: list[str] = []
    for row in facts.rules:
        line = f"- {row['label']}: {row['status']}"
        if digit_audit(line, facts.allowed_runs):
            continue
        lines.append(line)
    return "\n".join(lines) if lines else "Rules recorded."


def _gaps_body(facts: ReportFacts, extra: list[str]) -> str:
    lines: list[str] = []
    for gap in [*facts.context_gaps, *extra]:
        text = str(gap).strip()
        if not text or digit_audit(text, facts.allowed_runs):
            continue
        lines.append(f"- {text}")
    return "\n".join(lines) if lines else "None."


def render_debrief(
    record: SessionRecord,
    facts: ReportFacts,
    prose: ReportProse,
    *,
    root: Path,
    provider: ReportProvider,
) -> DebriefReport:
    gaps: list[str] = []
    day = _keep_span(prose.day, facts, gaps, "day")
    brief_spans = [
        kept
        for span in prose.brief_vs_behaviour
        if (kept := _keep_span(span, facts, gaps, "brief vs behaviour"))
    ]
    obs_spans = [
        kept for span in prose.observations if (kept := _keep_span(span, facts, gaps, "observations"))
    ]
    learn_spans = [
        kept for span in prose.learnings if (kept := _keep_span(span, facts, gaps, "learnings"))
    ]
    if len(learn_spans) < 3:
        for extra in _fallback_learnings(facts):
            kept = _keep_span(extra, facts, gaps, "learnings")
            if kept is None:
                continue
            if kept.text not in {item.text for item in learn_spans}:
                learn_spans.append(kept)
            if len(learn_spans) == 3:
                break
    if len(learn_spans) < 3:
        gaps.append("fewer than three candidate learnings")
    clips = _evidence_clips(root, record.id)
    day_body, day_cites = _join_spans([day] if day else [])
    brief_body, brief_cites = _join_spans(brief_spans)
    obs_body, obs_cites = _join_spans(obs_spans)
    learn_body, learn_cites = _join_spans(learn_spans)
    bodies = {
        "source": _source_body(facts, record),
        "day": day_body,
        "trades": _trades_body(facts, clips),
        "rules": _rules_body(facts),
        "brief_vs_behaviour": brief_body,
        "observations": obs_body,
        "learnings": learn_body,
        "gaps": _gaps_body(facts, gaps),
    }
    cites = {
        "source": [],
        "day": day_cites,
        "trades": [
            *[row["tva_trade_id"] for row in facts.trades],
            *[clip for clip in clips.values() if clip],
        ],
        "rules": [row["rule"] for row in facts.rules],
        "brief_vs_behaviour": brief_cites,
        "observations": obs_cites,
        "learnings": learn_cites,
        "gaps": [],
    }
    sections = [
        DebriefSection(
            id=sid,
            title=title,
            kind=kind,  # type: ignore[arg-type]
            body=_safe_text(bodies[sid], facts.allowed_runs),
            cites=cites[sid],
        )
        for sid, title, kind in SECTION_SPECS
    ]
    return DebriefReport(
        schema_version=SCHEMA_VERSION,
        session_id=record.id,
        provider=provider.name,
        model=provider.model,
        prompt_version=provider.prompt_version,
        sections=sections,
        gaps=gaps,
    )


def render_markdown(report: DebriefReport) -> str:
    parts = ["# Debrief", ""]
    for section in report.sections:
        parts.append(f"## {section.title}")
        parts.append("")
        parts.append(section.body or "")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def build_debrief(
    record: SessionRecord,
    *,
    root: Path,
    provider_name: str | None = None,
    client: httpx.Client | None = None,
) -> DebriefReport:
    facts = gather_facts(record, root=root)
    provider = get_report_provider(provider_name, client=client)
    prose = provider.prose(facts)
    return render_debrief(record, facts, prose, root=root, provider=provider)


def report_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
    client: httpx.Client | None = None,
) -> ReportResult:
    session_id = _require_safe_session_id(session_id)
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    report = build_debrief(record, root=root, provider_name=provider_name, client=client)
    markdown = render_markdown(report)
    bad = digit_audit(markdown, collect_allowed_runs(root, session_id))
    if bad:
        store.drop_debrief(root, session_id)
        store.compute_status(root, session_id)
        raise ReportError("debrief.md digit not in trades/ocr/context: " + ", ".join(bad[:8]))
    store.debrief_md_path(root, session_id).write_text(markdown, encoding="utf-8")
    store.write_json(store.debrief_json_path(root, session_id), report.model_dump(mode="json"))
    store.compute_status(root, session_id)
    return ReportResult(
        session_id=session_id,
        status="ok",
        path="debrief.md",
        json_path="debrief.json",
        provider=report.provider,
        gaps=len(report.gaps),
    )
