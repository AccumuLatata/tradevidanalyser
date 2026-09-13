"""Notion briefs/DRC + ThesisTester lab parquet join (PR-22). Never recomputes levels."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx
import pyarrow.parquet as pq

from tradevidanalyser import store
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.schema import (
    BriefContext,
    DrcContext,
    LabContext,
    LabTradeContext,
    SessionContext,
    SessionRecord,
)

SCHEMA_VERSION = "1"
ENV_NOTION_KEY = "NOTION_API_KEY"
ENV_NOTION_PROVIDER = "TVA_NOTION_PROVIDER"
ENV_JOURNAL_DB = "TVA_NOTION_JOURNAL_DB"
ENV_LAB_DIR = "TVA_LAB_DIR"
NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
HTTP_TIMEOUT = 30.0
NOTION_PAGE_SIZE = 100
NOTION_MAX_PAGES = 50
_NOTION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    r"|^[0-9a-fA-F]{32}$"
)
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_LAB_FILES = {
    "attribution": ("attribution.parquet", "journal_attribution.parquet"),
    "zones": ("zones.parquet", "journal_zones.parquet"),
    "triggers": ("triggers.parquet", "journal_triggers.parquet"),
}
_BIAS_NQ = ("bias_nq", "bias nq", "nq bias")
_BIAS_ES = ("bias_es", "bias es", "es bias")
_CONVICTION = ("conviction",)
_KILL = ("kill_levels", "kill levels", "kills")
_LABEL_LINE = re.compile(r"^\s*([^:]{1,40}):\s*(.+?)\s*$")


@dataclass(frozen=True)
class NotionPage:
    title: str
    url: str
    properties: dict[str, str] = field(default_factory=dict)
    body: str = ""
    database_id: str | None = None


class NotionClient(Protocol):
    def find_page(self, title: str) -> NotionPage | None: ...


@dataclass(frozen=True)
class ContextResult:
    session_id: str
    status: str
    path: str | None = None
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
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


class ContextError(ValueError):
    """Unreadable Notion response or lab-dir."""


def brief_day_label(day: date) -> str:
    """``<D Mon YYYY>`` with an English month and no leading zero on the day."""
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def macro_brief_title(day: date) -> str:
    return f"{brief_day_label(day)} Macro Brief"


def ny_brief_title(day: date) -> str:
    return f"{brief_day_label(day)} NY session Brief"


def drc_title(day: date) -> str:
    return f"DRC {day.strftime('%d%m%Y')}"


def session_calendar_date(record: SessionRecord) -> date:
    """Vienna calendar date of the recording. Naive stamps are Vienna (align).

    Prefer ``start_wallclock_vienna`` so a UTC-stored instant still maps to the
    desk clock. Session-id ``YYYY-MM-DD_…`` is the fallback when the wallclock
    is unreadable — never ``datetime.date()`` on a UTC value (that drops TZ).
    """
    raw = record.recording.start_wallclock_vienna
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=VIENNA)
        return parsed.astimezone(VIENNA).date()
    try:
        return date.fromisoformat(record.id[:10])
    except ValueError as exc:
        raise ContextError(f"unreadable session date on {record.id!r}") from exc


def _require_safe_session_id(session_id: str) -> str:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return session_id


def _cell(value: object) -> str | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN
            return None
    except TypeError:
        pass
    if isinstance(value, (list, tuple)):
        parts = [_cell(item) for item in value]
        joined = ",".join(part for part in parts if part)
        return joined or None
    text = str(value).strip()
    if not text or text.lower() in {"none", "nan", "nat", "<na>"}:
        return None
    return text


def _norm_title(text: str) -> str:
    return " ".join((text or "").split())


def _pick_labeled(page: NotionPage, aliases: tuple[str, ...]) -> str | None:
    props = {key.strip().lower(): value for key, value in page.properties.items() if value}
    for alias in aliases:
        if alias in props and props[alias]:
            return props[alias]
    blob = f"{page.body or ''}"
    for raw in blob.splitlines():
        match = _LABEL_LINE.match(raw)
        if not match:
            continue
        label = match.group(1).strip().lower()
        if label in aliases:
            text = match.group(2).strip()
            if text:
                return text
    return None


def _first_labeled(*pages: NotionPage | None, aliases: tuple[str, ...]) -> str | None:
    for page in pages:
        if page is None:
            continue
        found = _pick_labeled(page, aliases)
        if found:
            return found
    return None


def _brief_from_pages(macro: NotionPage | None, ny: NotionPage | None) -> BriefContext | None:
    if macro is None and ny is None:
        return None
    return BriefContext(
        macro_url=macro.url if macro is not None else None,
        ny_url=ny.url if ny is not None else None,
        bias_nq=_first_labeled(ny, macro, aliases=_BIAS_NQ),
        bias_es=_first_labeled(ny, macro, aliases=_BIAS_ES),
        conviction=_first_labeled(ny, macro, aliases=_CONVICTION),
        kill_levels=_first_labeled(ny, macro, aliases=_KILL),
        quoted=True,
    )


def _drc_from_page(page: NotionPage) -> DrcContext:
    scores: dict[str, str] = {}
    for key, value in page.properties.items():
        lowered = key.strip().lower()
        if lowered in {"name", "title"} or not value:
            continue
        scores[key] = value
    if not scores:
        for raw in (page.body or "").splitlines():
            match = _LABEL_LINE.match(raw)
            if match and match.group(2).strip():
                scores[match.group(1).strip()] = match.group(2).strip()
    return DrcContext(scores=scores, url=page.url)


@dataclass
class FakeNotionClient:
    pages: list[NotionPage] = field(default_factory=list)

    def find_page(self, title: str) -> NotionPage | None:
        want = _norm_title(title)
        for page in self.pages:
            if _norm_title(page.title) == want:
                return page
        return None


class LiveNotionClient:
    def __init__(
        self,
        *,
        api_key: str,
        database_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ContextError("NOTION_API_KEY is unset")
        self._key = api_key.strip()
        raw_db = (database_id or "").strip()
        if raw_db and _NOTION_ID.match(raw_db) is None:
            raise ContextError("TVA_NOTION_JOURNAL_DB is not a Notion id")
        self._database_id = raw_db or None
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def find_page(self, title: str) -> NotionPage | None:
        own = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            if self._database_id:
                try:
                    found = self._query_database(client, title)
                    if found is not None:
                        return found
                except (ContextError, httpx.HTTPError):
                    # Bad/unknown DB must not hide workspace search.
                    pass
            return self._search_title(client, title)
        except httpx.HTTPError as exc:
            raise ContextError(f"Notion request failed: {exc}") from exc
        finally:
            if own:
                client.close()

    def _iter_pages(
        self,
        client: httpx.Client,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        method: str = "POST",
    ) -> list[object]:
        results: list[object] = []
        cursor: str | None = None
        for _ in range(NOTION_MAX_PAGES):
            if method == "POST":
                body = dict(json_body or {})
                body["page_size"] = NOTION_PAGE_SIZE
                if cursor:
                    body["start_cursor"] = cursor
                response = client.post(url, headers=self._headers(), json=body)
            else:
                query = dict(params or {})
                query["page_size"] = NOTION_PAGE_SIZE
                if cursor:
                    query["start_cursor"] = cursor
                response = client.get(url, headers=self._headers(), params=query)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ContextError("Notion returned a non-object")
            chunk = payload.get("results") or []
            if isinstance(chunk, list):
                results.extend(chunk)
            if not payload.get("has_more"):
                break
            nxt = payload.get("next_cursor")
            if not nxt:
                break
            cursor = str(nxt)
        return results

    def _page_matches(self, page: NotionPage, title: str) -> bool:
        if _norm_title(page.title) != _norm_title(title):
            return False
        if not self._database_id:
            return True
        if not page.database_id:
            return True
        return page.database_id.replace("-", "").lower() == self._database_id.replace(
            "-", ""
        ).lower()

    def _query_database(self, client: httpx.Client, title: str) -> NotionPage | None:
        url = f"{NOTION_API}/databases/{self._database_id}/query"
        last_error: httpx.HTTPError | None = None
        for prop in ("Name", "Title"):
            try:
                raws = self._iter_pages(
                    client,
                    url,
                    json_body={
                        "filter": {"property": prop, "title": {"equals": title}},
                    },
                )
            except httpx.HTTPStatusError as exc:
                last_error = exc
                if exc.response is not None and exc.response.status_code in {400, 404}:
                    continue
                raise
            for raw in raws:
                page = self._hydrate(client, raw)
                if page is not None and self._page_matches(page, title):
                    return page
            return None
        if last_error is not None:
            raise last_error
        return None

    def _search_title(self, client: httpx.Client, title: str) -> NotionPage | None:
        raws = self._iter_pages(
            client,
            f"{NOTION_API}/search",
            json_body={
                "query": title,
                "filter": {"value": "page", "property": "object"},
            },
        )
        for raw in raws:
            page = self._hydrate(client, raw)
            if page is not None and self._page_matches(page, title):
                return page
        return None

    def _hydrate(self, client: httpx.Client, raw: object) -> NotionPage | None:
        if not isinstance(raw, dict) or raw.get("object") != "page":
            return None
        page_id = str(raw.get("id") or "")
        if not page_id:
            return None
        title = _notion_title(raw)
        url = str(raw.get("url") or "")
        props = _flatten_properties(raw.get("properties") or {})
        body = self._page_body(client, page_id)
        parent = raw.get("parent") or {}
        database_id = None
        if isinstance(parent, dict):
            database_id = parent.get("database_id")
        return NotionPage(
            title=_norm_title(title),
            url=url,
            properties=props,
            body=body,
            database_id=str(database_id) if database_id else None,
        )

    def _page_body(self, client: httpx.Client, page_id: str) -> str:
        try:
            blocks = self._iter_pages(
                client,
                f"{NOTION_API}/blocks/{page_id}/children",
                method="GET",
            )
        except httpx.HTTPStatusError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return ""
            raise
        lines: list[str] = []
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text = _block_text(block)
            if text:
                lines.append(text)
        return "\n".join(lines)


def _rich_text(items: object) -> str:
    if not isinstance(items, list):
        return ""
    parts: list[str] = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("plain_text")
            if text:
                parts.append(str(text))
    return "".join(parts)


def _notion_title(raw: dict) -> str:
    props = raw.get("properties") or {}
    if isinstance(props, dict):
        for value in props.values():
            if isinstance(value, dict) and value.get("type") == "title":
                text = _rich_text(value.get("title"))
                if text:
                    return text
    return ""


def _flatten_properties(props: object) -> dict[str, str]:
    if not isinstance(props, dict):
        return {}
    out: dict[str, str] = {}
    for name, value in props.items():
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        text = ""
        if kind == "title":
            text = _rich_text(value.get("title"))
        elif kind == "rich_text":
            text = _rich_text(value.get("rich_text"))
        elif kind == "select":
            select = value.get("select") or {}
            if isinstance(select, dict):
                text = str(select.get("name") or "")
        elif kind == "status":
            status = value.get("status") or {}
            if isinstance(status, dict):
                text = str(status.get("name") or "")
        elif kind == "multi_select":
            names = [
                str(item.get("name"))
                for item in (value.get("multi_select") or [])
                if isinstance(item, dict) and item.get("name")
            ]
            text = ", ".join(names)
        elif kind == "number" and value.get("number") is not None:
            text = str(value.get("number"))
        elif kind == "formula":
            formula = value.get("formula") or {}
            if isinstance(formula, dict):
                ftype = formula.get("type")
                if ftype == "string" and formula.get("string"):
                    text = str(formula.get("string"))
                elif ftype == "number" and formula.get("number") is not None:
                    text = str(formula.get("number"))
        elif kind == "url" and value.get("url"):
            text = str(value.get("url"))
        if text:
            out[str(name)] = text
    return out


def _block_text(block: dict) -> str:
    kind = str(block.get("type") or "")
    payload = block.get(kind)
    if not isinstance(payload, dict):
        return ""
    return _rich_text(payload.get("rich_text"))


def get_notion_client(
    provider_name: str | None = None,
    *,
    pages: list[NotionPage] | None = None,
    client: httpx.Client | None = None,
) -> NotionClient:
    name = (provider_name or os.environ.get(ENV_NOTION_PROVIDER) or "fake").strip().lower()
    if not name or name in {"fake", "test", "mock"}:
        return FakeNotionClient(pages=list(pages or []))
    key = (os.environ.get(ENV_NOTION_KEY) or "").strip()
    if not key:
        raise ContextError("NOTION_API_KEY unset")
    return LiveNotionClient(
        api_key=key,
        database_id=os.environ.get(ENV_JOURNAL_DB),
        client=client,
    )


def resolve_lab_dir(lab_dir: Path | str | None) -> Path | None:
    raw = lab_dir if lab_dir is not None else (os.environ.get(ENV_LAB_DIR) or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_file():
        path = path.parent
    if not path.is_dir():
        raise ContextError(f"lab-dir is not a directory: {path}")
    return path


def _find_lab_file(lab_dir: Path, names: tuple[str, ...]) -> Path | None:
    for name in names:
        path = lab_dir / name
        if path.is_file():
            return path
    return None


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    table = pq.read_table(path)
    columns = table.column_names
    rows: list[dict[str, Any]] = []
    for index in range(table.num_rows):
        rows.append({name: table.column(name)[index].as_py() for name in columns})
    return rows


def _fill_key(row: dict[str, Any]) -> str | None:
    return _cell(row.get("entry_fill_id")) or _cell(row.get("fill_id"))


def _match_lab_row(trade: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Join on ``entry_fill_id`` first; ``trade_id`` only as the documented mapping.

    A first-row ``trade_id`` hit must not beat a later ``entry_fill_id`` hit, and
    a conflicting lab ``entry_fill_id`` must not fall back to ``trade_id``.
    """
    fill = _fill_key(trade)
    trade_id = _cell(trade.get("trade_id"))
    if fill:
        for row in rows:
            lab_fill = _fill_key(row)
            if lab_fill and lab_fill == fill:
                return row
        for row in rows:
            lab_trade = _cell(row.get("trade_id"))
            if lab_trade and lab_trade == fill:
                return row
        for row in rows:
            lab_fill = _fill_key(row)
            if lab_fill and trade_id and lab_fill == trade_id:
                return row
    if trade_id:
        for row in rows:
            lab_trade = _cell(row.get("trade_id"))
            lab_fill = _fill_key(row)
            if not lab_trade or lab_trade != trade_id:
                continue
            if fill and lab_fill and lab_fill != fill:
                continue
            return row
    return None


def _read_session_trades(root: Path, session_id: str) -> list[dict[str, Any]]:
    path = store.trades_path(root, session_id)
    if not path.is_file():
        return []
    rows = _read_parquet_rows(path)
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        tva_id = _cell(row.get("tva_trade_id")) or _cell(row.get("trade_id")) or f"T{index + 1:02d}"
        out.append(
            {
                "tva_trade_id": tva_id,
                "trade_id": _cell(row.get("trade_id")),
                "entry_fill_id": _fill_key(row),
            }
        )
    return out


def _join_lab(
    trades: list[dict[str, Any]],
    lab_dir: Path,
) -> tuple[LabContext | None, list[str]]:
    gaps: list[str] = []
    tables: dict[str, list[dict[str, Any]]] = {}
    missing: list[str] = []
    for kind, names in _LAB_FILES.items():
        path = _find_lab_file(lab_dir, names)
        if path is None:
            missing.append(kind)
            continue
        tables[kind] = _read_parquet_rows(path)
    if not tables:
        gaps.append(f"lab parquet missing under {lab_dir}")
        return None, gaps
    if missing:
        gaps.append("lab parquet missing: " + ", ".join(missing))
    if not trades:
        gaps.append("no trades.parquet")
        return None, gaps
    per_trade: dict[str, LabTradeContext] = {}
    for trade in trades:
        tva_id = str(trade["tva_trade_id"])
        attr = _match_lab_row(trade, tables.get("attribution") or [])
        zone = _match_lab_row(trade, tables.get("zones") or [])
        trig = _match_lab_row(trade, tables.get("triggers") or [])
        if attr is None and zone is None and trig is None:
            gaps.append(f"no lab row for {tva_id}")
        per_trade[tva_id] = LabTradeContext(
            nearest_level_token=_cell((attr or {}).get("nearest_level_token")),
            level_context=_cell((attr or {}).get("level_context")),
            tag_alignment=_cell((attr or {}).get("tag_alignment")),
            inferred_triggers_1m=_cell((trig or {}).get("inferred_triggers_1m")),
            zone_id=_cell((zone or {}).get("zone_id")),
        )
    return LabContext(per_trade=per_trade), gaps


def build_context(
    record: SessionRecord,
    *,
    root: Path,
    notion: NotionClient | None = None,
    lab_dir: Path | None = None,
    notion_provider: str | None = None,
) -> SessionContext:
    session_id = _require_safe_session_id(record.id)
    day = session_calendar_date(record)
    gaps: list[str] = []
    brief: BriefContext | None = None
    drc: DrcContext | None = None
    # Live Notion without a key must fail the stage (not write brief:null).
    client = notion if notion is not None else get_notion_client(notion_provider)
    try:
        macro = client.find_page(macro_brief_title(day))
        ny = client.find_page(ny_brief_title(day))
        brief = _brief_from_pages(macro, ny)
        if brief is None:
            gaps.append(f"brief not found for {brief_day_label(day)}")
        else:
            if macro is None:
                gaps.append(f"{macro_brief_title(day)} not found")
            if ny is None:
                gaps.append(f"{ny_brief_title(day)} not found")
            if not any(
                (brief.bias_nq, brief.bias_es, brief.conviction, brief.kill_levels)
            ):
                gaps.append("brief has no quoted bias/conviction/kill levels")
        drc_page = client.find_page(drc_title(day))
        if drc_page is not None:
            drc = _drc_from_page(drc_page)
        else:
            gaps.append(f"{drc_title(day)} not found")
    except ContextError as exc:
        gaps.append(str(exc))
        brief = None
        drc = None
    lab: LabContext | None = None
    resolved = resolve_lab_dir(lab_dir)
    if resolved is None:
        gaps.append("lab-dir not set")
    else:
        trades = _read_session_trades(root, session_id)
        lab, lab_gaps = _join_lab(trades, resolved)
        gaps.extend(lab_gaps)
    return SessionContext(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        brief=brief,
        drc=drc,
        lab=lab,
        gaps=gaps,
    )


def context_session(
    session_id: str,
    *,
    root: Path,
    lab_dir: Path | None = None,
    notion: NotionClient | None = None,
    notion_provider: str | None = None,
) -> ContextResult:
    session_id = _require_safe_session_id(session_id)
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    report = build_context(
        record,
        root=root,
        notion=notion,
        lab_dir=lab_dir,
        notion_provider=notion_provider,
    )
    store.write_json(store.context_path(root, session_id), report.model_dump(mode="json"))
    # Brief / lab facts feed the debrief; a rewrite must not leave yesterday's prose.
    store.drop_debrief(root, session_id)
    store.compute_status(root, session_id)
    return ContextResult(
        session_id=session_id,
        status="ok",
        path="context.json",
        gaps=len(report.gaps),
    )
