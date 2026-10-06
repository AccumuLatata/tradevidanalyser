"""One Notion page per day (PR-34, plan §2.5)."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from tradevidanalyser import store
from tradevidanalyser.day_manifest import (
    clips_for_day,
    current_pointer_path,
    day_dir,
    load_current_day_json,
    nominal_vienna_date,
)
from tradevidanalyser.flags import day_publish_enabled
from tradevidanalyser.publish import (
    SCHEMA_VERSION,
    TAG,
    DebriefPayload,
    PublishError,
    PublishResult,
    _load_debrief,
    _nonempty_learning_lines,
    _page_matches_title,
    _page_usable,
    _run_log_line,
    _section_body,
    _titles_match,
    debrief_title,
    get_publish_client,
)
from tradevidanalyser.rules import DAY_RULE_IDS
from tradevidanalyser.schema import (
    PublishRecord,
    SessionRecord,
    alignment_is_invalid,
)

PUBLISH_NAME = "publish.json"
DEBRIEF_MISSING_PREFIX = "Debrief fehlt für "


def day_publish_path(root: Path, day: date) -> Path:
    return day_dir(root, day) / PUBLISH_NAME


def debrief_missing_error(session_ids: list[str]) -> str:
    return f"{DEBRIEF_MISSING_PREFIX}{', '.join(session_ids)}; tva report ausführen"


def missing_debrief_ids(root: Path, clips: list[SessionRecord]) -> list[str]:
    return [record.id for record in clips if not store.debrief_json_path(root, record.id).is_file()]


def compose_day_summaries(
    *,
    clip_count: int,
    suspected_count: int,
    minute_clock_count: int,
    alignment_invalid: bool,
    rule_statuses: list[tuple[str, str]],
    trades_outside_clips: int,
) -> str:
    parts = [
        f"{clip_count} clips",
        f"{suspected_count} suspected",
        f"{minute_clock_count} clock_resolution_s=60",
    ]
    if alignment_invalid:
        parts.append("alignment_invalid")
    for rule, status in rule_statuses:
        parts.append(f"{rule} {status}")
    parts.append(f"trades_outside_clips {trades_outside_clips}")
    return ", ".join(parts) + "."


def payload_from_day(
    root: Path,
    day: date,
    clips: list[SessionRecord],
    record: SessionRecord,
) -> DebriefPayload:
    return DebriefPayload(
        title=debrief_title(record),
        summaries=day_summaries_sentence(root, day, clips),
        learnings=_day_learning_lines(root, clips),
    )


def day_summaries_sentence(
    root: Path,
    day: date,
    clips: list[SessionRecord] | None = None,
) -> str:
    clips = clips if clips is not None else clips_for_day(root, day)
    payload = load_current_day_json(root, day) or {}
    rows = [row for row in (payload.get("clips") or []) if isinstance(row, dict)]
    clip_count = len(rows) if rows else len(clips)
    suspected_count = sum(1 for row in rows if row.get("pause_check") == "suspected")
    minute_clock_count = sum(1 for row in rows if _is_minute_clock(row.get("clock_resolution_s")))
    alignment_invalid = _any_alignment_invalid(clips, rows)
    statuses = _day_rule_statuses(root, day)
    trades_outside = payload.get("trades_outside_clips")
    try:
        outside = int(trades_outside or 0)
    except (TypeError, ValueError):
        outside = 0
    return compose_day_summaries(
        clip_count=clip_count,
        suspected_count=suspected_count,
        minute_clock_count=minute_clock_count,
        alignment_invalid=alignment_invalid,
        rule_statuses=statuses,
        trades_outside_clips=outside,
    )


def publish_session_day_path(
    record: SessionRecord,
    *,
    root: Path,
    provider_name: str | None = None,
    client: httpx.Client | None = None,
    now: datetime | None = None,
) -> PublishResult:
    """Write one Notion page for the day. page_id lives under days/<date>/."""
    if not day_publish_enabled():
        raise PublishError("TVA_DAY_PUBLISH ist aus")
    day = nominal_vienna_date(record)
    clips = clips_for_day(root, day)
    missing = missing_debrief_ids(root, clips)
    if missing:
        raise PublishError(debrief_missing_error(missing))
    payload = payload_from_day(root, day, clips, record)
    publisher = get_publish_client(provider_name, root=root, client=client)
    existing_id = _saved_day_page_id(root, day, title=payload.title)
    title_page = publisher.find_page(payload.title, database_only=True)
    if title_page is None:
        title_page = publisher.find_page(payload.title, database_only=False)
    created = False
    replaced = False
    page = None
    if existing_id:
        current = publisher.get_page(existing_id)
        if (
            _page_usable(current)
            and current is not None
            and _page_matches_title(current, payload.title)
        ):
            page = publisher.update_debrief(existing_id, payload)
    if page is None:
        if title_page is not None:
            page = publisher.update_debrief(title_page.page_id, payload)
            if existing_id is None:
                replaced = True
        else:
            page = publisher.create_debrief(payload)
            created = True
    log_line = _run_log_line(record.id, when=now)
    publisher.append_run_log(log_line, session_id=record.id)
    _write_day_publish(
        root,
        day,
        page_id=page.page_id,
        page_url=page.url,
        payload=payload,
        provider=publisher.name,
        log_line=log_line,
        created=created,
    )
    artifact = PublishRecord(
        schema_version=SCHEMA_VERSION,
        session_id=record.id,
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
    store.write_json(store.publish_path(root, record.id), artifact.model_dump(mode="json"))
    store.compute_status(root, record.id)
    return PublishResult(
        session_id=record.id,
        status="ok",
        path="publish.json",
        page_id=page.page_id,
        page_url=page.url,
        created=created,
        provider=publisher.name,
        replaced_existing_page=replaced,
    )


def _day_learning_lines(root: Path, clips: list[SessionRecord]) -> list[str]:
    collected: list[str] = []
    for clip in clips:
        report = _load_debrief(root, clip.id)
        collected.extend(_nonempty_learning_lines(_section_body(report, "learnings")))
        if len(collected) >= 3:
            break
    while len(collected) < 3:
        collected.append("")
    return collected[:3]


def _any_alignment_invalid(
    clips: list[SessionRecord],
    rows: list[dict[str, Any]],
) -> bool:
    for row in rows:
        if row.get("pause_check") == "suspected":
            return True
        if row.get("alignment_method") == "invalid":
            return True
    return any(alignment_is_invalid(clip.alignment) for clip in clips)


def _is_minute_clock(value: Any) -> bool:
    if value is None:
        return False
    try:
        return int(value) == 60
    except (TypeError, ValueError):
        return False


def _day_rule_statuses(root: Path, day: date) -> list[tuple[str, str]]:
    found: dict[str, str] = {}
    pointer = current_pointer_path(root, day)
    build_id = None
    if pointer.is_file():
        try:
            build_id = pointer.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            build_id = None
    if build_id:
        path = day_dir(root, day) / "builds" / build_id / "rules.json"
        if path.is_file():
            try:
                body = store.read_json(path)
            except (OSError, ValueError):
                body = None
            if isinstance(body, dict):
                for item in body.get("rules") or []:
                    if isinstance(item, dict) and item.get("rule") and item.get("status"):
                        found[str(item["rule"])] = str(item["status"])
    return [(rule, found.get(rule, "unverifiable")) for rule in DAY_RULE_IDS]


def _saved_day_page_id(root: Path, day: date, *, title: str) -> str | None:
    path = day_publish_path(root, day)
    if not path.is_file():
        return None
    try:
        raw = store.read_json(path)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    saved_title = str(raw.get("title") or "")
    if saved_title and not _titles_match(saved_title, title):
        return None
    page_id = str(raw.get("page_id") or "").strip()
    return page_id or None


def _write_day_publish(
    root: Path,
    day: date,
    *,
    page_id: str,
    page_url: str,
    payload: DebriefPayload,
    provider: str,
    log_line: str,
    created: bool,
) -> None:
    dest = day_publish_path(root, day)
    dest.parent.mkdir(parents=True, exist_ok=True)
    store.write_json(
        dest,
        {
            "schema_version": SCHEMA_VERSION,
            "date": day.isoformat(),
            "provider": provider,
            "page_id": page_id,
            "page_url": page_url,
            "title": payload.title,
            "tag": TAG,
            "summaries": payload.summaries,
            "learnings": list(payload.learnings),
            "log_line": log_line,
            "created": created,
        },
    )
