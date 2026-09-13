"""Spoken playbook/levels → ThesisTester tag_map vocabulary (PR-26)."""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import yaml

from tradevidanalyser import store
from tradevidanalyser.schema import (
    Evidence,
    IntentProposal,
    IntentProposals,
    Insights,
    ProposalStatus,
    SessionRecord,
)

SCHEMA_VERSION = "1"
ENV_TAG_MAP = "TVA_TAG_MAP"
CSV_COLUMNS = ("date", "symbol", "side", "price", "quantity", "tags", "notes")
NOTES_PREFIX = "[TVA proposed]"
_WORD = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*")


class ProposalsError(ValueError):
    """Missing tag map or an unknown proposal id."""


@dataclass(frozen=True)
class TagMap:
    exact: dict[str, str]
    context: frozenset[str]
    by_fold: dict[str, str]
    qualifiers: tuple[str, ...] = ()

    @property
    def vocabulary(self) -> frozenset[str]:
        return frozenset(self.exact) | self.context


@dataclass(frozen=True)
class ProposalsResult:
    session_id: str
    status: str
    proposals: int = 0
    path: str | None = None
    csv_path: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "session_id": self.session_id,
            "proposals": self.proposals,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.csv_path is not None:
            payload["csv_path"] = self.csv_path
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def default_tag_map_path() -> Path:
    env = (os.environ.get(ENV_TAG_MAP) or "").strip()
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise FileNotFoundError(f"{ENV_TAG_MAP}={env!r} is not a file")
        return path
    try:
        import thesistester.journal.tags as tt_tags

        bundled = Path(tt_tags._MAP_PATH)
        if bundled.is_file():
            return bundled
    except (ImportError, AttributeError):
        pass
    here = Path(__file__).resolve().parent
    candidates = [here / "tag_map.yaml"]
    if len(here.parents) >= 2:
        candidates.append(here.parents[1] / "tag_map.yaml")
    candidates.append(Path.cwd() / "tag_map.yaml")
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.expanduser()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved
    raise FileNotFoundError("tag_map.yaml not found (set TVA_TAG_MAP or keep the shipped file)")


def load_tag_map(path: Path | None = None) -> TagMap:
    source = path or default_tag_map_path()
    try:
        payload = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ProposalsError(f"tag map is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise ProposalsError("tag map must be a mapping")
    exact_raw = payload.get("exact")
    if not isinstance(exact_raw, dict):
        raise ProposalsError("tag map missing exact table")
    exact = {str(key): str(key) for key in exact_raw if key}
    context = frozenset(str(item) for item in (payload.get("context") or ()) if item)
    qualifiers = tuple(
        sorted(
            (str(item) for item in (payload.get("qualifiers") or ()) if item),
            key=len,
            reverse=True,
        )
    )
    by_fold: dict[str, str] = {}
    for key in (*exact, *context):
        by_fold.setdefault(key.casefold(), key)
    return TagMap(exact=exact, context=context, by_fold=by_fold, qualifiers=qualifiers)


def resolve_stated_token(token: str, tag_map: TagMap) -> str | None:
    """Map one token to a desk tag key. Qualifier suffixes strip after exact hits."""
    blob = (token or "").strip()
    if not blob:
        return None
    hit = tag_map.by_fold.get(blob.casefold())
    if hit:
        return hit
    for suffix in tag_map.qualifiers:
        if len(blob) > len(suffix) and blob.casefold().endswith(suffix.casefold()):
            base = blob[: -len(suffix)]
            hit = tag_map.by_fold.get(base.casefold())
            if hit:
                return hit
    return None


def map_stated_text(text: str, tag_map: TagMap) -> list[str]:
    """Return canonical desk tags found in *text*. Unknown words are dropped."""
    blob = (text or "").strip()
    if not blob:
        return []
    found: list[str] = []
    seen: set[str] = set()

    def add(tag: str) -> None:
        if tag and tag not in seen and tag in tag_map.vocabulary:
            seen.add(tag)
            found.append(tag)

    whole = tag_map.by_fold.get(blob.casefold())
    if whole:
        add(whole)
        return found
    compact = re.sub(r"[^a-z0-9]", "", blob.casefold())
    if compact:
        for key in sorted(tag_map.vocabulary, key=len, reverse=True):
            if re.sub(r"[^a-z0-9]", "", key.casefold()) == compact:
                add(key)
                return found
    hits: list[tuple[int, int, str]] = []
    for key in tag_map.vocabulary:
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])" + re.escape(key) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        for match in pattern.finditer(blob):
            hits.append((match.start(), -len(key), key))
    for _, _, key in sorted(hits):
        add(key)
    for token in _WORD.findall(blob):
        hit = resolve_stated_token(token, tag_map)
        if hit:
            add(hit)
    return found


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _load_evidence(root: Path, session_id: str) -> Evidence | None:
    path = store.evidence_path(root, session_id)
    if not path.is_file():
        return None
    try:
        return Evidence.model_validate(store.read_json(path))
    except (ValueError, OSError):
        return None


def _load_insights(root: Path, session_id: str) -> Insights | None:
    path = store.insights_path(root, session_id)
    if not path.is_file():
        return None
    try:
        return store.load_insights(root, session_id)
    except (ValueError, OSError):
        return None


def _trade_ids(root: Path, session_id: str) -> list[str]:
    path = store.trades_path(root, session_id)
    if not path.is_file():
        return []
    table = pq.read_table(path)
    names = set(table.column_names)
    if "tva_trade_id" in names:
        return [str(value) for value in table.column("tva_trade_id").to_pylist() if value]
    if "trade_id" in names:
        return [str(value) for value in table.column("trade_id").to_pylist() if value]
    return []


def _cell(table, name: str, index: int) -> object:
    if name not in table.column_names:
        return None
    return table.column(name)[index].as_py()


def _as_date(value: object, fallback: str) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date().isoformat()
        except (TypeError, ValueError):
            pass
    text = str(value or "").strip()
    if len(text) >= 10 and text[4] == "-":
        return text[:10]
    return fallback[:10]


def build_proposals(
    record: SessionRecord,
    *,
    root: Path,
    tag_map: TagMap | None = None,
) -> IntentProposals:
    session_id = record.id
    mapping = tag_map or load_tag_map()
    previous = _previous_status(root, session_id)
    evidence = _load_evidence(root, session_id)
    insights = _load_insights(root, session_id)
    trade_ids = _trade_ids(root, session_id)
    if evidence is not None:
        for trade in evidence.trades:
            if trade.tva_trade_id and trade.tva_trade_id not in trade_ids:
                trade_ids.append(trade.tva_trade_id)
    if not trade_ids:
        trade_ids = ["T01"] if (evidence or insights) else []
    gaps: list[str] = []
    proposals: list[IntentProposal] = []
    session_level_tags: list[tuple[str, str]] = []
    if insights is not None:
        for span in insights.stated_levels:
            tags = map_stated_text(span.token or span.text, mapping)
            if not tags:
                raw = span.token or span.text
                if raw:
                    gaps.append(f"unmapped stated_level {raw}")
            for tag in tags:
                session_level_tags.append((tag, span.seg))
        for span in insights.playbooks_mentioned:
            tags = map_stated_text(span.name or span.text, mapping)
            if not tags:
                raw = span.name or span.text
                if raw:
                    gaps.append(f"unmapped playbook {raw}")
            for tag in tags:
                session_level_tags.append((tag, span.seg))
    evidence_by_id = {trade.tva_trade_id: trade for trade in (evidence.trades if evidence else [])}
    for tva_id in trade_ids:
        tags: list[str] = []
        segs: list[str] = []
        spoken: list[str] = []
        trade = evidence_by_id.get(tva_id)
        if trade is not None:
            for cite in (trade.stated.playbook, trade.stated.setup):
                if cite is None or not cite.value:
                    continue
                spoken.append(cite.value)
                mapped = map_stated_text(cite.value, mapping)
                if not mapped:
                    gaps.append(f"unmapped stated {cite.value!r} on {tva_id}")
                tags.extend(mapped)
                if cite.seg:
                    segs.append(cite.seg)
        spoken_blob = " ".join(spoken)
        for tag, seg in session_level_tags:
            if len(trade_ids) == 1 or (tag.casefold() in spoken_blob.casefold()):
                tags.append(tag)
                if seg:
                    segs.append(seg)
        tags = _unique(tags)
        segs = _unique(segs)
        if not tags:
            continue
        proposals.append(
            IntentProposal(
                tva_trade_id=tva_id,
                proposed_tags=tags,
                source_segs=segs,
                status=previous.get(tva_id, "proposed"),
            )
        )
    if not trade_ids:
        gaps.append("no trades.parquet or evidence.json")
    return IntentProposals(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        proposals=proposals,
        gaps=_unique(gaps),
    )


def _previous_status(root: Path, session_id: str) -> dict[str, ProposalStatus]:
    path = store.proposals_path(root, session_id)
    if not path.is_file():
        return {}
    try:
        data = store.read_json(path)
    except (ValueError, OSError):
        return {}
    out: dict[str, ProposalStatus] = {}
    for item in data.get("proposals") or []:
        if not isinstance(item, dict):
            continue
        tva_id = str(item.get("tva_trade_id") or "")
        status = item.get("status")
        if tva_id and status in {"proposed", "confirmed", "rejected"}:
            out[tva_id] = status
    return out


def write_tradesviz_csv(
    root: Path,
    session_id: str,
    report: IntentProposals,
) -> Path:
    path = store.tradesviz_tags_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _csv_rows(root, session_id, report)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def _csv_rows(root: Path, session_id: str, report: IntentProposals) -> list[dict[str, str]]:
    by_id = {item.tva_trade_id: item for item in report.proposals if item.status != "rejected"}
    if not by_id:
        return []
    fallback_date = session_id[:10]
    path = store.trades_path(root, session_id)
    rows: list[dict[str, str]] = []
    if path.is_file():
        table = pq.read_table(path)
        n = table.num_rows
        for index in range(n):
            tva_id = str(_cell(table, "tva_trade_id", index) or _cell(table, "trade_id", index) or "")
            proposal = by_id.get(tva_id)
            if proposal is None:
                continue
            rows.append(
                {
                    "date": _as_date(_cell(table, "session_date", index), fallback_date),
                    "symbol": str(_cell(table, "instrument", index) or ""),
                    "side": str(_cell(table, "direction", index) or ""),
                    "price": _csv_number(_cell(table, "entry_price", index)),
                    "quantity": _csv_number(_cell(table, "qty", index) or 1),
                    "tags": ",".join(proposal.proposed_tags),
                    "notes": f"{NOTES_PREFIX} {tva_id}",
                }
            )
        return rows
    for proposal in report.proposals:
        if proposal.status == "rejected":
            continue
        rows.append(
            {
                "date": fallback_date,
                "symbol": "",
                "side": "",
                "price": "",
                "quantity": "1",
                "tags": ",".join(proposal.proposed_tags),
                "notes": f"{NOTES_PREFIX} {proposal.tva_trade_id}",
            }
        )
    return rows


def _csv_number(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def proposals_session(session_id: str, *, root: Path) -> ProposalsResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    report = build_proposals(record, root=root)
    store.write_json(store.proposals_path(root, session_id), report.model_dump(mode="json"))
    write_tradesviz_csv(root, session_id, report)
    store.compute_status(root, session_id)
    return ProposalsResult(
        session_id=session_id,
        status="ok",
        proposals=len(report.proposals),
        path="intent_proposals.json",
        csv_path="tradesviz_tags.csv",
    )


def _parse_proposal_id(ident: str) -> tuple[str | None, str]:
    text = ident.strip()
    if ":" in text:
        session_id, _, trade_id = text.partition(":")
        return session_id.strip(), trade_id.strip()
    if "/" in text:
        session_id, _, trade_id = text.partition("/")
        return session_id.strip(), trade_id.strip()
    return None, text


def find_proposal(root: Path, ident: str) -> tuple[str, IntentProposals, IntentProposal]:
    session_id, trade_id = _parse_proposal_id(ident)
    if not trade_id:
        raise ProposalsError(f"unreadable proposal id {ident!r}")
    if session_id:
        if not store.is_safe_path_name(session_id):
            raise ValueError(f"unsafe session id {session_id!r}")
        path = store.proposals_path(root, session_id)
        if not path.is_file():
            raise ProposalsError(f"no proposals for {session_id}")
        report = IntentProposals.model_validate(store.read_json(path))
        for item in report.proposals:
            if item.tva_trade_id == trade_id:
                return session_id, report, item
        raise ProposalsError(f"proposal {trade_id} not in {session_id}")
    hits: list[tuple[str, IntentProposals, IntentProposal]] = []
    for sid in store.list_session_ids(root):
        path = store.proposals_path(root, sid)
        if not path.is_file():
            continue
        report = IntentProposals.model_validate(store.read_json(path))
        for item in report.proposals:
            if item.tva_trade_id == trade_id:
                hits.append((sid, report, item))
    if not hits:
        raise ProposalsError(f"proposal {trade_id} not found")
    if len(hits) > 1:
        raise ProposalsError(f"proposal {trade_id} is ambiguous; use session_id:{trade_id}")
    return hits[0]


def toggle_status(current: ProposalStatus) -> ProposalStatus:
    if current == "proposed":
        return "confirmed"
    if current == "confirmed":
        return "proposed"
    return "proposed"


def set_proposal_status(
    ident: str,
    *,
    root: Path,
    status: ProposalStatus | None = None,
    toggle: bool = False,
) -> ProposalsResult:
    session_id, report, item = find_proposal(root, ident)
    if toggle:
        item.status = toggle_status(item.status)
    elif status is not None:
        item.status = status
    else:
        raise ProposalsError("set_proposal_status needs status or toggle=True")
    store.write_json(store.proposals_path(root, session_id), report.model_dump(mode="json"))
    write_tradesviz_csv(root, session_id, report)
    store.compute_status(root, session_id)
    return ProposalsResult(
        session_id=session_id,
        status=item.status,
        proposals=len(report.proposals),
        path="intent_proposals.json",
        csv_path="tradesviz_tags.csv",
    )


def confirm_proposal(ident: str, *, root: Path) -> ProposalsResult:
    return set_proposal_status(ident, root=root, toggle=True)
