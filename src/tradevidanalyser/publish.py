"""Optional Notion Session Debrief publish (PR-25). Off by default."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

import httpx

from tradevidanalyser import store
from tradevidanalyser.context import (
    ENV_JOURNAL_DB,
    ENV_NOTION_KEY,
    ENV_NOTION_PROVIDER,
    NOTION_API,
    NOTION_MAX_PAGES,
    NOTION_PAGE_SIZE,
    NOTION_VERSION,
    brief_day_label,
    session_calendar_date,
)
from tradevidanalyser.naming import VIENNA
from tradevidanalyser.schema import DebriefReport, PublishRecord, SessionRecord

SCHEMA_VERSION = "1"
ENV_RUNS_PAGE = "TVA_NOTION_RUNS_PAGE"
HTTP_TIMEOUT = 30.0
TAG = "Trades Summary"
RUNS_TITLE = "TVA runs"
_NOTION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    r"|^[0-9a-fA-F]{32}$"
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


class PublishError(ValueError):
    """Missing debrief, missing --notion, or Notion write failure."""


@dataclass(frozen=True)
class DebriefPayload:
    title: str
    summaries: str
    learnings: list[str]


@dataclass
class PublishedPage:
    page_id: str
    url: str
    title: str
    properties: dict[str, str] = field(default_factory=dict)
    body: str = ""
    archived: bool = False
    database_id: str | None = None


class PublishNotionClient(Protocol):
    name: str

    def find_page(self, title: str, *, database_only: bool = False) -> PublishedPage | None: ...

    def get_page(self, page_id: str) -> PublishedPage | None: ...

    def create_debrief(self, payload: DebriefPayload) -> PublishedPage: ...

    def update_debrief(self, page_id: str, payload: DebriefPayload) -> PublishedPage: ...

    def append_run_log(self, line: str, *, session_id: str) -> str | None: ...


@dataclass(frozen=True)
class PublishResult:
    session_id: str
    status: str
    path: str | None = None
    page_id: str | None = None
    page_url: str | None = None
    created: bool = False
    provider: str | None = None
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "session_id": self.session_id,
            "created": self.created,
        }
        if self.path is not None:
            payload["path"] = self.path
        if self.page_id is not None:
            payload["page_id"] = self.page_id
        if self.page_url is not None:
            payload["page_url"] = self.page_url
        if self.provider is not None:
            payload["provider"] = self.provider
        if self.reason is not None:
            payload["reason"] = self.reason
        return payload


def debrief_title(record: SessionRecord) -> str:
    return f"{brief_day_label(session_calendar_date(record))} Session Debrief"


def _one_sentence(text: str) -> str:
    compact = " ".join((text or "").split())
    if not compact:
        return ""
    parts = _SENTENCE.split(compact, maxsplit=1)
    return parts[0].strip()


def _learning_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip().lstrip("-*").strip()
        if line:
            lines.append(line)
        if len(lines) == 3:
            break
    while len(lines) < 3:
        lines.append("")
    return lines[:3]


def _section_body(report: DebriefReport, section_id: str) -> str:
    for section in report.sections:
        if section.id == section_id:
            return section.body or ""
    return ""


def payload_from_debrief(record: SessionRecord, report: DebriefReport) -> DebriefPayload:
    summaries = _one_sentence(_section_body(report, "day"))
    if not summaries:
        summaries = _one_sentence(_section_body(report, "observations"))
    if not summaries:
        summaries = "Session debrief."
    return DebriefPayload(
        title=debrief_title(record),
        summaries=summaries,
        learnings=_learning_lines(_section_body(report, "learnings")),
    )


def _run_log_line(session_id: str, *, when: datetime | None = None) -> str:
    stamp = (when or datetime.now(VIENNA)).strftime("%Y-%m-%d %H:%M")
    return f"{stamp} Vienna | session {session_id} | publish ok"


def _norm_title(text: str) -> str:
    return " ".join((text or "").split())


def _titles_match(left: str, right: str) -> bool:
    return _norm_title(left) == _norm_title(right)


def _looks_like_notion_id(value: str) -> bool:
    return bool(value) and _NOTION_ID.match(value.strip()) is not None


def _page_usable(page: PublishedPage | None) -> bool:
    return page is not None and not page.archived


def _rich(text: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": (text or "")[:1900]}}]


class FakePublishClient:
    name = "fake"

    def __init__(self, root: Path, pages: dict[str, PublishedPage] | None = None) -> None:
        self._root = root
        self._pages = pages if pages is not None else _load_fake_pages(root)

    def _persist(self) -> None:
        _save_fake_pages(self._root, self._pages)

    def find_page(self, title: str, *, database_only: bool = False) -> PublishedPage | None:
        want = _norm_title(title)
        for page in self._pages.values():
            if page.archived:
                continue
            if _titles_match(page.title, want):
                return page
        return None

    def get_page(self, page_id: str) -> PublishedPage | None:
        page = self._pages.get(page_id)
        if page is None or page.archived:
            return None
        return page

    def create_debrief(self, payload: DebriefPayload) -> PublishedPage:
        existing = self.find_page(payload.title)
        if existing is not None:
            return self.update_debrief(existing.page_id, payload)
        page_id = f"fake-{uuid4().hex[:12]}"
        page = PublishedPage(
            page_id=page_id,
            url=f"https://notion.fake/{page_id}",
            title=payload.title,
            properties=_payload_props(payload),
        )
        self._pages[page_id] = page
        self._persist()
        return page

    def update_debrief(self, page_id: str, payload: DebriefPayload) -> PublishedPage:
        current = self._pages.get(page_id)
        if current is None:
            return self.create_debrief(payload)
        updated = PublishedPage(
            page_id=page_id,
            url=current.url,
            title=payload.title,
            properties=_payload_props(payload),
            body=current.body,
        )
        self._pages[page_id] = updated
        self._persist()
        return updated

    def append_run_log(self, line: str, *, session_id: str) -> str | None:
        runs = self.find_page(RUNS_TITLE)
        if runs is None:
            page_id = f"fake-runs-{uuid4().hex[:8]}"
            runs = PublishedPage(
                page_id=page_id,
                url=f"https://notion.fake/{page_id}",
                title=RUNS_TITLE,
            )
            self._pages[page_id] = runs
        if _log_already_present(runs.body, session_id):
            self._persist()
            return runs.page_id
        runs.body = f"{line}\n{runs.body}".rstrip() + "\n"
        self._persist()
        return runs.page_id


class LivePublishClient:
    name = "notion"

    def __init__(
        self,
        *,
        api_key: str,
        database_id: str | None = None,
        runs_page_id: str | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise PublishError("NOTION_API_KEY is unset. Set it or use TVA_NOTION_PROVIDER=fake.")
        self._key = api_key.strip()
        raw_db = (database_id or "").strip()
        if raw_db and _NOTION_ID.match(raw_db) is None:
            raise PublishError("TVA_NOTION_JOURNAL_DB is not a Notion id")
        self._database_id = raw_db or None
        raw_runs = (runs_page_id or "").strip()
        if raw_runs and _NOTION_ID.match(raw_runs) is None:
            raise PublishError("TVA_NOTION_RUNS_PAGE is not a Notion id")
        self._runs_page_id = raw_runs or None
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _client_ctx(self) -> httpx.Client:
        return self._client or httpx.Client(timeout=HTTP_TIMEOUT)

    def find_page(self, title: str, *, database_only: bool = False) -> PublishedPage | None:
        own = self._client is None
        client = self._client_ctx()
        try:
            if self._database_id:
                found = self._query_database(client, title)
                if found is not None:
                    return found
                if database_only:
                    return None
            return self._search_title(client, title)
        except httpx.HTTPError as exc:
            raise PublishError(f"Notion request failed: {exc}") from exc
        finally:
            if own and self._client is None:
                client.close()

    def get_page(self, page_id: str) -> PublishedPage | None:
        if not _looks_like_notion_id(page_id):
            return None
        own = self._client is None
        client = self._client_ctx()
        try:
            response = client.get(
                f"{NOTION_API}/pages/{page_id.strip()}", headers=self._headers()
            )
            if response.status_code in {400, 404}:
                return None
            if response.status_code >= 400:
                raise PublishError(f"Notion HTTP {response.status_code}: {response.text[:300]}")
            page = _page_from_raw(response.json())
            if not _page_usable(page):
                return None
            return page
        except httpx.HTTPError as exc:
            raise PublishError(f"Notion request failed: {exc}") from exc
        finally:
            if own and self._client is None:
                client.close()

    def create_debrief(self, payload: DebriefPayload) -> PublishedPage:
        if not self._database_id:
            raise PublishError("TVA_NOTION_JOURNAL_DB is unset")
        existing = self.find_page(payload.title, database_only=True)
        if existing is not None:
            patched = self._patch_debrief(existing.page_id, payload)
            if patched is not None:
                return patched
        return self._post_debrief(payload)

    def update_debrief(self, page_id: str, payload: DebriefPayload) -> PublishedPage:
        patched = self._patch_debrief(page_id, payload)
        if patched is not None:
            return patched
        existing = self.find_page(payload.title, database_only=bool(self._database_id))
        if existing is not None and existing.page_id != page_id:
            patched = self._patch_debrief(existing.page_id, payload)
            if patched is not None:
                return patched
        return self._post_debrief(payload)

    def _post_debrief(self, payload: DebriefPayload) -> PublishedPage:
        if not self._database_id:
            raise PublishError("TVA_NOTION_JOURNAL_DB is unset")
        own = self._client is None
        client = self._client_ctx()
        try:
            props = self._properties(client, payload)
            response = client.post(
                f"{NOTION_API}/pages",
                headers=self._headers(),
                json={"parent": {"database_id": self._database_id}, "properties": props},
            )
            if response.status_code >= 400:
                raise PublishError(f"Notion HTTP {response.status_code}: {response.text[:300]}")
            page = _page_from_raw(response.json())
            if page is None:
                raise PublishError("Notion create returned a non-page")
            return page
        except httpx.HTTPError as exc:
            raise PublishError(f"Notion request failed: {exc}") from exc
        finally:
            if own and self._client is None:
                client.close()

    def _patch_debrief(self, page_id: str, payload: DebriefPayload) -> PublishedPage | None:
        if not _looks_like_notion_id(page_id):
            return None
        own = self._client is None
        client = self._client_ctx()
        try:
            props = self._properties(client, payload)
            response = client.patch(
                f"{NOTION_API}/pages/{page_id.strip()}",
                headers=self._headers(),
                json={"properties": props},
            )
            if response.status_code in {400, 404}:
                return None
            if response.status_code >= 400:
                raise PublishError(f"Notion HTTP {response.status_code}: {response.text[:300]}")
            page = _page_from_raw(response.json())
            if page is None:
                raise PublishError("Notion update returned a non-page")
            return page
        except httpx.HTTPError as exc:
            raise PublishError(f"Notion request failed: {exc}") from exc
        finally:
            if own and self._client is None:
                client.close()

    def append_run_log(self, line: str, *, session_id: str) -> str | None:
        page_id = self._runs_page_id
        if not page_id:
            found = self.find_page(RUNS_TITLE)
            page_id = found.page_id if found else None
        if not page_id or not _looks_like_notion_id(page_id):
            return None
        own = self._client is None
        client = self._client_ctx()
        try:
            try:
                blocks = _iter_notion_results(
                    client,
                    f"{NOTION_API}/blocks/{page_id.strip()}/children",
                    headers=self._headers(),
                    method="GET",
                )
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in {400, 404}:
                    return None
                raise
            texts = [_block_plain_text(block) for block in blocks]
            if _log_already_present("\n".join(texts), session_id):
                return page_id
            response = client.post(
                f"{NOTION_API}/blocks/{page_id.strip()}/children",
                headers=self._headers(),
                json={
                    "children": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {"rich_text": _rich(line)},
                        }
                    ]
                },
            )
            if response.status_code >= 400:
                raise PublishError(f"Notion HTTP {response.status_code}: {response.text[:300]}")
            return page_id
        except httpx.HTTPError as exc:
            raise PublishError(f"Notion request failed: {exc}") from exc
        finally:
            if own and self._client is None:
                client.close()

    def _properties(self, client: httpx.Client, payload: DebriefPayload) -> dict[str, Any]:
        title_key, tag_key, summary_key, learn_keys = self._property_names(client)
        props: dict[str, Any] = {
            title_key: {"title": _rich(payload.title)},
            tag_key: {"multi_select": [{"name": TAG}]},
            summary_key: {"rich_text": _rich(payload.summaries)},
        }
        for key, text in zip(learn_keys, payload.learnings, strict=False):
            props[key] = {"rich_text": _rich(text)}
        return props

    def _property_names(self, client: httpx.Client) -> tuple[str, str, str, list[str]]:
        title_key, tag_key, summary_key = "Name", "Tags", "Summaries"
        learn_keys = ["Learning 1", "Learning 2", "Learning 3"]
        if not self._database_id:
            return title_key, tag_key, summary_key, learn_keys
        try:
            response = client.get(
                f"{NOTION_API}/databases/{self._database_id}", headers=self._headers()
            )
        except httpx.HTTPError:
            return title_key, tag_key, summary_key, learn_keys
        if response.status_code >= 400:
            return title_key, tag_key, summary_key, learn_keys
        raw = response.json()
        props = raw.get("properties") if isinstance(raw, dict) else None
        if not isinstance(props, dict):
            return title_key, tag_key, summary_key, learn_keys
        for name, spec in props.items():
            if not isinstance(spec, dict):
                continue
            kind = spec.get("type")
            lower = str(name).strip().lower()
            if kind == "title":
                title_key = str(name)
            elif kind == "multi_select" and "tag" in lower:
                tag_key = str(name)
            elif kind == "rich_text" and "summar" in lower:
                summary_key = str(name)
            elif kind == "rich_text" and lower in {"learning 1", "learning1"}:
                learn_keys[0] = str(name)
            elif kind == "rich_text" and lower in {"learning 2", "learning2"}:
                learn_keys[1] = str(name)
            elif kind == "rich_text" and lower in {"learning 3", "learning3"}:
                learn_keys[2] = str(name)
        return title_key, tag_key, summary_key, learn_keys

    def _query_database(self, client: httpx.Client, title: str) -> PublishedPage | None:
        url = f"{NOTION_API}/databases/{self._database_id}/query"
        title_key = self._property_names(client)[0]
        props: list[str] = []
        for prop in (title_key, "Name", "Title"):
            if prop and prop not in props:
                props.append(prop)
        for prop in props:
            try:
                raws = _iter_notion_results(
                    client,
                    url,
                    headers=self._headers(),
                    json_body={"filter": {"property": prop, "title": {"equals": title}}},
                )
            except PublishError:
                raise
            except httpx.HTTPStatusError as exc:
                if exc.response is not None and exc.response.status_code in {400, 404}:
                    continue
                raise PublishError(
                    f"Notion HTTP {exc.response.status_code if exc.response is not None else '?'}: "
                    f"{(exc.response.text[:300] if exc.response is not None else str(exc))}"
                ) from exc
            for raw in raws:
                page = _page_from_raw(raw)
                if _page_usable(page) and page is not None and _titles_match(page.title, title):
                    return page
            return None
        return None

    def _search_title(self, client: httpx.Client, title: str) -> PublishedPage | None:
        raws = _iter_notion_results(
            client,
            f"{NOTION_API}/search",
            headers=self._headers(),
            json_body={
                "query": title,
                "filter": {"value": "page", "property": "object"},
            },
        )
        for raw in raws:
            page = _page_from_raw(raw)
            if _page_usable(page) and page is not None and _titles_match(page.title, title):
                if self._database_id and page.database_id:
                    if _norm_id(page.database_id) != _norm_id(self._database_id):
                        continue
                return page
        return None


def _payload_props(payload: DebriefPayload) -> dict[str, str]:
    return {
        "title": payload.title,
        "Tags": TAG,
        "Summaries": payload.summaries,
        "Learning 1": payload.learnings[0] if payload.learnings else "",
        "Learning 2": payload.learnings[1] if len(payload.learnings) > 1 else "",
        "Learning 3": payload.learnings[2] if len(payload.learnings) > 2 else "",
    }


def _log_already_present(text: str, session_id: str) -> bool:
    """True when a *TVA runs* line already records this session's publish."""
    marker = f"session {session_id}"
    for line in (text or "").splitlines():
        compact = " ".join(line.split())
        if marker in compact and "publish ok" in compact.lower():
            return True
    return False


def _norm_id(value: str) -> str:
    return value.replace("-", "").lower()


def _block_plain_text(block: object) -> str:
    if not isinstance(block, dict):
        return ""
    kind = str(block.get("type") or "")
    data = block.get(kind) if kind else None
    if not isinstance(data, dict):
        return ""
    return "".join(
        str(item.get("plain_text") or "")
        for item in (data.get("rich_text") or [])
        if isinstance(item, dict)
    )


def _iter_notion_results(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    method: str = "POST",
    json_body: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> list[object]:
    results: list[object] = []
    cursor: str | None = None
    for _ in range(NOTION_MAX_PAGES):
        if method == "POST":
            body = dict(json_body or {})
            body["page_size"] = NOTION_PAGE_SIZE
            if cursor:
                body["start_cursor"] = cursor
            response = client.post(url, headers=headers, json=body)
        else:
            query = dict(params or {})
            query["page_size"] = NOTION_PAGE_SIZE
            if cursor:
                query["start_cursor"] = cursor
            response = client.get(url, headers=headers, params=query)
        if response.status_code >= 400:
            response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PublishError("Notion returned a non-object")
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


def _page_from_raw(raw: object) -> PublishedPage | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("object")
    if kind not in {None, "page"}:
        return None
    page_id = str(raw.get("id") or "")
    if not page_id:
        return None
    title = ""
    props = raw.get("properties") or {}
    if isinstance(props, dict):
        for value in props.values():
            if isinstance(value, dict) and value.get("type") == "title":
                title = "".join(
                    str(item.get("plain_text") or "")
                    for item in (value.get("title") or [])
                    if isinstance(item, dict)
                )
                if title:
                    break
    parent = raw.get("parent") or {}
    database_id = None
    if isinstance(parent, dict) and parent.get("database_id"):
        database_id = str(parent.get("database_id"))
    archived = bool(raw.get("archived") or raw.get("in_trash"))
    return PublishedPage(
        page_id=page_id,
        url=str(raw.get("url") or ""),
        title=_norm_title(title),
        archived=archived,
        database_id=database_id,
    )


def _load_fake_pages(root: Path) -> dict[str, PublishedPage]:
    path = store.notion_fake_path(root)
    if not path.is_file():
        return {}
    try:
        data = store.read_json(path)
    except (ValueError, OSError):
        return {}
    pages: dict[str, PublishedPage] = {}
    for item in data.get("pages") or []:
        if not isinstance(item, dict) or not item.get("page_id"):
            continue
        page = PublishedPage(
            page_id=str(item["page_id"]),
            url=str(item.get("url") or ""),
            title=str(item.get("title") or ""),
            properties=dict(item.get("properties") or {}),
            body=str(item.get("body") or ""),
            archived=bool(item.get("archived")),
            database_id=str(item["database_id"]) if item.get("database_id") else None,
        )
        pages[page.page_id] = page
    return pages


def _save_fake_pages(root: Path, pages: dict[str, PublishedPage]) -> None:
    store.write_json(
        store.notion_fake_path(root),
        {
            "pages": [
                {
                    "page_id": page.page_id,
                    "url": page.url,
                    "title": page.title,
                    "properties": page.properties,
                    "body": page.body,
                    "archived": page.archived,
                    "database_id": page.database_id,
                }
                for page in pages.values()
            ]
        },
    )


def get_publish_client(
    name: str | None = None,
    *,
    root: Path,
    client: httpx.Client | None = None,
) -> PublishNotionClient:
    chosen = (name or os.environ.get(ENV_NOTION_PROVIDER) or "fake").strip().lower()
    if chosen in {"fake", "test", "mock"}:
        return FakePublishClient(root)
    if chosen in {"notion", "live"}:
        key = (os.environ.get(ENV_NOTION_KEY) or "").strip()
        if not key:
            raise PublishError("NOTION_API_KEY is unset. Set it or use TVA_NOTION_PROVIDER=fake.")
        return LivePublishClient(
            api_key=key,
            database_id=os.environ.get(ENV_JOURNAL_DB),
            runs_page_id=os.environ.get(ENV_RUNS_PAGE),
            client=client,
        )
    raise PublishError(f"unknown Notion publish provider {chosen!r}")


def _load_debrief(root: Path, session_id: str) -> DebriefReport:
    path = store.debrief_json_path(root, session_id)
    if not path.is_file():
        raise PublishError("debrief.json is missing; run tva report first")
    report = DebriefReport.model_validate(store.read_json(path))
    if report.session_id != session_id:
        raise PublishError(
            f"debrief.json session_id {report.session_id!r} does not match {session_id!r}"
        )
    return report


def _saved_page_id(root: Path, session_id: str, *, title: str) -> str | None:
    saved = store.publish_path(root, session_id)
    if not saved.is_file():
        return None
    try:
        raw = store.read_json(saved)
    except (ValueError, OSError):
        return None
    if not isinstance(raw, dict):
        return None
    saved_session = str(raw.get("session_id") or "")
    if saved_session and saved_session != session_id:
        return None
    saved_title = str(raw.get("title") or "")
    if saved_title and not _titles_match(saved_title, title):
        return None
    page_id = str(raw.get("page_id") or "").strip()
    return page_id or None


def _page_matches_title(page: PublishedPage, title: str) -> bool:
    if not page.title:
        return True
    return _titles_match(page.title, title)


def publish_session(
    session_id: str,
    *,
    root: Path,
    notion: bool = False,
    provider_name: str | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> PublishResult:
    if not notion:
        raise PublishError("publish is off by default; pass --notion")
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    if record.id != session_id:
        raise ValueError(
            f"session.json id {record.id!r} does not match directory {session_id!r}"
        )
    report = _load_debrief(root, session_id)
    payload = payload_from_debrief(record, report)
    publisher = get_publish_client(provider_name, root=root, client=client)
    existing_id = _saved_page_id(root, session_id, title=payload.title)
    created = False
    page = None
    if existing_id:
        current = publisher.get_page(existing_id)
        if _page_usable(current) and current is not None and _page_matches_title(current, payload.title):
            page = publisher.update_debrief(existing_id, payload)
    if page is None:
        found = publisher.find_page(payload.title, database_only=True)
        if found is not None:
            page = publisher.update_debrief(found.page_id, payload)
        else:
            page = publisher.create_debrief(payload)
            created = True
    log_line = _run_log_line(session_id, when=now)
    publisher.append_run_log(log_line, session_id=session_id)
    artifact = PublishRecord(
        schema_version=SCHEMA_VERSION,
        session_id=session_id,
        provider=publisher.name,
        page_id=page.page_id,
        page_url=page.url,
        title=payload.title,
        tag=TAG,
        summaries=payload.summaries,
        learnings=list(payload.learnings),
        log_line=log_line,
        created=created,
    )
    store.write_json(store.publish_path(root, session_id), artifact.model_dump(mode="json"))
    store.compute_status(root, session_id)
    return PublishResult(
        session_id=session_id,
        status="ok",
        path="publish.json",
        page_id=page.page_id,
        page_url=page.url,
        created=created,
        provider=publisher.name,
    )
