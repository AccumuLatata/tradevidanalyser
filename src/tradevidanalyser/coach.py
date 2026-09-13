"""Cited weekly coach claims + at most one ledger experiment (PR-27)."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import httpx

from tradevidanalyser import store
from tradevidanalyser.ledger import (
    LedgerError,
    append_experiment,
    list_experiments,
    window_row_ids,
    window_rule_facts,
    window_session_ids,
    window_trade_facts,
)
from tradevidanalyser.naming import vienna_today
from tradevidanalyser.providers.extract import default_prompt_path, prompt_version_for
from tradevidanalyser.schema import (
    CoachClaim,
    CoachExperiment,
    CoachReport,
    DebriefReport,
)

SCHEMA_VERSION = "1"
ENV_COACH = "TVA_COACH_PROVIDER"
ENV_COACH_MODEL = "TVA_COACH_MODEL"
ENV_COACH_MIN_N = "TVA_COACH_MIN_N"
ENV_XAI_KEY = "XAI_API_KEY"
PROMPT_FILENAME = "coach_v1.md"
PROMPT_VERSION_FAKE = "coach-fake-v1"
DEFAULT_GROK_MODEL = "grok-4.6"
DEFAULT_MIN_N = 10
DEFAULT_WEEKS = 4
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
HTTP_TIMEOUT = 3600.0
DIGIT_RUN = re.compile(r"\d+")


class CoachError(ValueError):
    """Unreadable provider output or a thin citation set."""


@dataclass(frozen=True)
class CoachDraft:
    claims: list[CoachClaim] = field(default_factory=list)
    experiment: CoachExperiment | None = None


@dataclass(frozen=True)
class CoachPack:
    weeks: int
    min_citations: int
    session_ids: list[str]
    row_ids: list[str]
    trades: list[dict[str, Any]]
    rules: list[dict[str, Any]]
    debriefs: list[dict[str, str]]
    running: CoachExperiment | None
    existing: CoachExperiment | None
    allowed_runs: set[str]


@dataclass(frozen=True)
class CoachResult:
    status: str
    weeks: int
    claims: int = 0
    path: str | None = None
    md_path: str | None = None
    provider: str | None = None
    experiment_id: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "weeks": self.weeks,
            "claims": self.claims,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.md_path is not None:
            payload["md_path"] = self.md_path
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.experiment_id is not None:
            payload["experiment_id"] = self.experiment_id
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class CoachProvider(Protocol):
    name: str
    model: str
    prompt_version: str

    def review(self, pack: CoachPack) -> CoachDraft: ...


def min_citations(raw: str | None = None) -> int:
    text = (raw if raw is not None else os.environ.get(ENV_COACH_MIN_N) or "").strip()
    if not text:
        return DEFAULT_MIN_N
    try:
        value = int(text)
    except ValueError as exc:
        raise CoachError(f"{ENV_COACH_MIN_N} must be an int (got {text!r})") from exc
    if value < 1:
        raise CoachError(f"{ENV_COACH_MIN_N} must be >= 1")
    return value


def _digit_runs(text: str) -> set[str]:
    return set(DIGIT_RUN.findall(text or ""))


def _collect_allowed_runs(pack_bits: list[str], *, min_n: int, weeks: int) -> set[str]:
    allowed = set(_digit_runs(str(min_n))) | set(_digit_runs(str(weeks)))
    for bit in pack_bits:
        allowed |= _digit_runs(bit)
    return allowed


def _load_debriefs(root: Path, session_ids: list[str], *, limit: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for session_id in reversed(session_ids):
        if not store.is_safe_path_name(session_id):
            continue
        try:
            path = store.require_under_root(store.debrief_json_path(root, session_id), root)
        except ValueError:
            continue
        if not path.is_file():
            continue
        try:
            report = DebriefReport.model_validate(store.read_json(path))
        except (ValueError, OSError):
            continue
        body = "\n".join(section.body for section in report.sections if section.body)
        out.append({"session_id": session_id, "body": body[:2000]})
        if len(out) >= limit:
            break
    out.reverse()
    return out


def instance_cite_ids(pack: CoachPack) -> set[str]:
    """Trade and rule row ids that count toward the ≥ N citation floor."""
    ids: set[str] = set()
    for row in (*pack.trades, *pack.rules):
        ident = row.get("id")
        if ident:
            ids.add(str(ident))
    return ids


def _existing_experiment(root: Path) -> tuple[CoachExperiment | None, CoachExperiment | None]:
    items = list_experiments(root)
    running = next((item for item in items if item.status == "running"), None)
    return running, running or (items[0] if items else None)


def gather_pack(root: Path, *, weeks: int, min_n: int) -> CoachPack:
    session_ids = window_session_ids(root, weeks=weeks)
    row_ids = window_row_ids(root, session_ids)
    trades = window_trade_facts(root, session_ids)
    rules = window_rule_facts(root, session_ids)
    debriefs = _load_debriefs(root, session_ids, limit=weeks)
    bits = [str(weeks), str(min_n), vienna_today().isoformat(), *session_ids, *row_ids]
    for row in (*trades, *rules):
        bits.extend(str(value) for value in row.values() if value is not None)
    for item in debriefs:
        bits.extend(item.values())
    running, existing = _existing_experiment(root)
    held = existing or running
    if held is not None:
        bits.extend([held.rule_change, held.start, held.stop_criterion, held.id])
    return CoachPack(
        weeks=weeks,
        min_citations=min_n,
        session_ids=session_ids,
        row_ids=row_ids,
        trades=trades,
        rules=rules,
        debriefs=debriefs,
        running=running,
        existing=existing,
        allowed_runs=_collect_allowed_runs(bits, min_n=min_n, weeks=weeks),
    )


def validate_claim(claim: CoachClaim, pack: CoachPack) -> str | None:
    allowed = set(pack.row_ids)
    cites = [cite for cite in claim.cites if cite]
    if any(cite not in allowed for cite in cites):
        return "fabricated ledger cite"
    unique: list[str] = []
    seen: set[str] = set()
    for cite in cites:
        if cite not in seen:
            seen.add(cite)
            unique.append(cite)
    instances = [cite for cite in unique if cite in instance_cite_ids(pack)]
    if len(instances) < pack.min_citations:
        return f"need >={pack.min_citations} ledger cites"
    extra = (_digit_runs(claim.text) | _digit_runs(claim.question)) - pack.allowed_runs
    # counts of cites are allowed (the claim is about N instances)
    extra -= _digit_runs(str(len(instances)))
    extra -= _digit_runs(str(len(unique)))
    extra -= _digit_runs(str(pack.min_citations))
    if extra:
        return f"invented digit run {sorted(extra)[0]}"
    if not (claim.text or "").strip():
        return "empty claim"
    return None


def accept_claims(draft: CoachDraft, pack: CoachPack) -> tuple[list[CoachClaim], list[str]]:
    kept: list[CoachClaim] = []
    gaps: list[str] = []
    for index, claim in enumerate(draft.claims, start=1):
        cites = []
        seen: set[str] = set()
        for cite in claim.cites:
            if cite and cite not in seen:
                seen.add(cite)
                cites.append(cite)
        cleaned = CoachClaim(text=claim.text.strip(), cites=cites, question=claim.question.strip())
        reason = validate_claim(cleaned, pack)
        if reason:
            gaps.append(f"dropped claim {index}: {reason}")
            continue
        kept.append(cleaned)
    return kept, gaps


def accept_experiment(draft: CoachDraft, pack: CoachPack) -> tuple[CoachExperiment | None, list[str]]:
    if pack.existing is not None:
        return pack.existing, []
    raw = draft.experiment
    if raw is None:
        return None, []
    rule = (raw.rule_change or "").strip()
    stop = (raw.stop_criterion or "").strip()
    start = (raw.start or "").strip()
    if not rule or not stop:
        return None, ["dropped experiment: need rule_change and stop_criterion"]
    if start:
        try:
            date.fromisoformat(start)
        except ValueError:
            return None, [f"dropped experiment: bad start {start!r}"]
    else:
        start = vienna_today().isoformat()
    extra = (_digit_runs(rule) | _digit_runs(stop) | _digit_runs(start)) - pack.allowed_runs
    extra -= _digit_runs(str(pack.min_citations))
    extra -= _digit_runs(str(pack.weeks))
    if extra:
        return None, [f"dropped experiment: invented digit run {sorted(extra)[0]}"]
    return CoachExperiment(rule_change=rule, start=start, stop_criterion=stop, status="running"), []


def render_markdown(report: CoachReport) -> str:
    lines = [
        "# Coach",
        "",
        f"Window: last {report.weeks} ISO week(s). "
        f"Each claim cites ≥ {report.min_citations} ledger rows.",
        "",
        "## Claims",
        "",
    ]
    if not report.claims:
        lines.append("No claim met the citation floor.")
        lines.append("")
    for index, claim in enumerate(report.claims, start=1):
        lines.append(f"{index}. {claim.text}")
        if claim.question:
            lines.append(f"   Question: {claim.question}")
        lines.append(f"   Cites: {', '.join(claim.cites)}")
        lines.append("")
    lines.extend(["## Experiment", ""])
    if report.experiment is None:
        lines.append("None.")
    else:
        exp = report.experiment
        lines.append(f"- id: {exp.id or '(pending)'}")
        lines.append(f"- status: {exp.status}")
        lines.append(f"- rule_change: {exp.rule_change}")
        lines.append(f"- start: {exp.start}")
        lines.append(f"- stop_criterion: {exp.stop_criterion}")
    if report.gaps:
        lines.extend(["", "## Gaps", ""])
        lines.extend(f"- {gap}" for gap in report.gaps)
    return "\n".join(lines).rstrip() + "\n"


class FakeCoachProvider:
    name = "fake"
    model = "none"
    prompt_version = PROMPT_VERSION_FAKE

    def __init__(self, draft: CoachDraft | None = None) -> None:
        self._draft = draft

    def review(self, pack: CoachPack) -> CoachDraft:
        if self._draft is not None:
            return self._draft
        cites = _preferred_cites(pack)
        claims: list[CoachClaim] = []
        if len(cites) >= pack.min_citations:
            n = len(cites)
            claims.append(
                CoachClaim(
                    text=f"The same process miss repeats across {n} ledger rows.",
                    cites=cites,
                    question="Name the playbook before the next entry?",
                )
            )
        experiment = pack.existing
        if experiment is None and claims:
            start = pack.session_ids[0][:10] if pack.session_ids else vienna_today().isoformat()
            experiment = CoachExperiment(
                rule_change="State the playbook before entry (R-PLAYBOOK)",
                start=start,
                stop_criterion=(
                    f"Stop after {pack.min_citations} sessions with R-PLAYBOOK pass "
                    f"or {pack.weeks} weeks"
                ),
            )
        return CoachDraft(claims=claims, experiment=experiment)


def _preferred_cites(pack: CoachPack) -> list[str]:
    groups: dict[str, list[str]] = {}
    for row in pack.rules:
        if row.get("status") == "violated" and row.get("id"):
            groups.setdefault(str(row["rule"]), []).append(str(row["id"]))
    ranked = sorted(groups.values(), key=len, reverse=True)
    if ranked and len(ranked[0]) >= pack.min_citations:
        return ranked[0]
    trade_ids = [str(row["id"]) for row in pack.trades if row.get("id")]
    if len(trade_ids) >= pack.min_citations:
        return trade_ids
    instances = [cite for cite in pack.row_ids if cite in instance_cite_ids(pack)]
    if len(instances) >= pack.min_citations:
        return instances
    return []


class GrokCoachProvider:
    name = "grok"
    model = DEFAULT_GROK_MODEL

    def __init__(self, client: httpx.Client | None = None, *, prompt_path: Path | None = None) -> None:
        self._client = client
        self.prompt_path = prompt_path
        env_model = (os.environ.get(ENV_COACH_MODEL) or "").strip()
        if env_model:
            self.model = env_model
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        self.prompt_version = prompt_version_for(path)

    def review(self, pack: CoachPack) -> CoachDraft:
        key = (os.environ.get(ENV_XAI_KEY) or "").strip()
        if not key:
            raise CoachError("XAI_API_KEY is unset. Set it or use TVA_COACH_PROVIDER=fake.")
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        body = self._complete(path.read_text(encoding="utf-8"), pack, key)
        claims: list[CoachClaim] = []
        for item in body.get("claims") or []:
            if not isinstance(item, dict):
                continue
            cites = [str(cite) for cite in (item.get("cites") or []) if cite]
            claims.append(
                CoachClaim(
                    text=str(item.get("text") or ""),
                    cites=cites,
                    question=str(item.get("question") or ""),
                )
            )
        raw_exp = body.get("experiment")
        experiment = None
        if isinstance(raw_exp, dict):
            experiment = CoachExperiment(
                rule_change=str(raw_exp.get("rule_change") or ""),
                start=str(raw_exp.get("start") or ""),
                stop_criterion=str(raw_exp.get("stop_criterion") or ""),
            )
        return CoachDraft(claims=claims, experiment=experiment)

    def _complete(self, system_prompt: str, pack: CoachPack, key: str) -> dict[str, Any]:
        user = {
            "weeks": pack.weeks,
            "min_citations": pack.min_citations,
            "allowed_ids": pack.row_ids,
            "trades": pack.trades,
            "rules": pack.rules,
            "debriefs": pack.debriefs,
            "running_experiment": pack.running.model_dump(mode="json") if pack.running else None,
            "allowed_digit_runs": sorted(pack.allowed_runs),
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "coach_review",
                    "strict": True,
                    "schema": _coach_schema(),
                },
            },
        }
        own = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            response = client.post(
                XAI_CHAT_URL,
                json=payload,
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise CoachError(f"Grok request failed: {exc}") from exc
        finally:
            if own:
                client.close()
        if response.status_code >= 400:
            raise CoachError(f"Grok HTTP {response.status_code}: {response.text[:300]}")
        try:
            raw = response.json()
        except ValueError as exc:
            raise CoachError("Grok returned non-JSON") from exc
        return _payload_body(raw)


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
        raise CoachError("Grok returned a non-object JSON body")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise CoachError("Grok response has no choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise CoachError("Grok choice is not an object")
    message = first.get("message")
    if not isinstance(message, dict):
        raise CoachError("Grok choice has no message")
    content = message.get("content")
    if isinstance(content, dict):
        return content
    raw = _message_content(message).strip()
    if not raw:
        refusal = message.get("refusal")
        if refusal:
            raise CoachError(f"Grok refused: {refusal}")
        raise CoachError("Grok message content is empty")
    try:
        body = json.loads(raw)
    except ValueError as exc:
        raise CoachError("Grok JSON content is not an object") from exc
    if not isinstance(body, dict):
        raise CoachError("Grok JSON content is not an object")
    return body


def _coach_schema() -> dict[str, Any]:
    claim = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "cites": {"type": "array", "items": {"type": "string"}},
            "question": {"type": "string"},
        },
        "required": ["text", "cites", "question"],
    }
    experiment = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rule_change": {"type": "string"},
            "start": {"type": "string"},
            "stop_criterion": {"type": "string"},
        },
        "required": ["rule_change", "start", "stop_criterion"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "claims": {"type": "array", "items": claim},
            "experiment": {"anyOf": [experiment, {"type": "null"}]},
        },
        "required": ["claims", "experiment"],
    }


def get_coach_provider(
    name: str | None = None,
    *,
    client: httpx.Client | None = None,
    draft: CoachDraft | None = None,
) -> CoachProvider:
    chosen = (name or os.environ.get(ENV_COACH) or "fake").strip().lower()
    if chosen in {"fake", "test"}:
        return FakeCoachProvider(draft)
    if chosen in {"grok", "xai"}:
        return GrokCoachProvider(client=client)
    raise CoachError(f"unknown coach provider {chosen!r}")


def load_latest(root: Path) -> CoachReport | None:
    try:
        path = store.require_under_root(store.coach_json_path(root), root)
    except ValueError:
        return None
    if not path.is_file():
        return None
    try:
        return CoachReport.model_validate(store.read_json(path))
    except (ValueError, OSError):
        return None


def coach_session(
    *,
    root: Path,
    weeks: int = DEFAULT_WEEKS,
    provider_name: str | None = None,
    min_n: int | None = None,
    client: httpx.Client | None = None,
    provider: CoachProvider | None = None,
) -> CoachResult:
    if weeks < 1:
        raise CoachError("weeks must be >= 1")
    floor = min_n if min_n is not None else min_citations()
    pack = gather_pack(root, weeks=weeks, min_n=floor)
    chosen = provider or get_coach_provider(provider_name, client=client)
    draft = chosen.review(pack)
    claims, gaps = accept_claims(draft, pack)
    experiment, exp_gaps = accept_experiment(draft, pack)
    gaps.extend(exp_gaps)
    if not claims and len(pack.row_ids) < floor:
        gaps.append(f"ledger window has {len(pack.row_ids)} citeable rows; need >={floor}")
    stored: CoachExperiment | None = None
    if experiment is not None and claims:
        try:
            stored = append_experiment(root, experiment)
        except LedgerError as exc:
            gaps.append(str(exc))
    elif experiment is not None and pack.existing is not None:
        stored = pack.existing
    report = CoachReport(
        schema_version=SCHEMA_VERSION,
        provider=chosen.name,
        model=chosen.model,
        prompt_version=chosen.prompt_version,
        weeks=weeks,
        min_citations=floor,
        claims=claims,
        experiment=stored,
        gaps=gaps,
    )
    report = report.model_copy(update={"markdown": render_markdown(report)})
    try:
        json_path = store.require_under_root(store.coach_json_path(root), root)
        md_path = store.require_under_root(store.coach_md_path(root), root)
    except ValueError as exc:
        raise CoachError(str(exc)) from exc
    store.write_json(json_path, report.model_dump(mode="json"))
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(report.markdown, encoding="utf-8")
    return CoachResult(
        status="ok",
        weeks=weeks,
        claims=len(claims),
        path="coach/latest.json",
        md_path="coach/latest.md",
        provider=chosen.name,
        experiment_id=stored.id if stored is not None else None,
        reason=None if claims else "no claim met the citation floor",
    )
