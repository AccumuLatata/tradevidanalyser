from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.context import ENV_JOURNAL_DB, ENV_NOTION_KEY, ENV_NOTION_PROVIDER
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.publish import (
    ENV_RUNS_PAGE,
    FakePublishClient,
    debrief_title,
    get_publish_client,
    payload_from_debrief,
    publish_session,
)
from tradevidanalyser.schema import (
    DebriefReport,
    DebriefSection,
    RecordingInfo,
    SessionRecord,
)
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app


def _session(
    root: Path,
    session_id: str = "2026-09-11_143000",
    *,
    start: str | None = None,
) -> SessionRecord:
    day = session_id[:10]
    clock = start or f"{day}T14:30:00+02:00"
    stamp = session_id[11:] if len(session_id) >= 15 else "143000"
    filename = f"{day} {stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{filename}",
            sha256="0" * 64,
            start_wallclock_vienna=clock,
            duration_s=3600.0,
            filename=filename,
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_debrief(root: Path, session_id: str, *, summaries: str | None = None) -> None:
    day = summaries or "Reviewed the tape against T01. Extra clause stays out."
    report = DebriefReport(
        session_id=session_id,
        provider="fake",
        model="none",
        prompt_version="debrief-fake-v1",
        sections=[
            DebriefSection(id="day", title="Day", kind="prose", body=day),
            DebriefSection(
                id="learnings",
                title="Learnings",
                kind="prose",
                body="Name the playbook before entry.\nState stop and target.\nCool down after tilt.",
            ),
        ],
    )
    store.write_json(store.debrief_json_path(root, session_id), report.model_dump(mode="json"))
    store.debrief_md_path(root, session_id).write_text("# Debrief\n", encoding="utf-8")


def test_payload_title_and_one_sentence(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    report = DebriefReport.model_validate(store.read_json(store.debrief_json_path(tva_root, record.id)))
    payload = payload_from_debrief(record, report)
    assert payload.title == "11 Sep 2026 Session Debrief"
    assert debrief_title(record) == payload.title
    assert payload.summaries == "Reviewed the tape against T01."
    assert payload.learnings == [
        "Name the playbook before entry.",
        "State stop and target.",
        "Cool down after tilt.",
    ]


def test_publish_requires_notion_flag(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    with pytest.raises(ValueError, match="--notion"):
        publish_session(record.id, root=tva_root, notion=False)
    assert main(["--root", str(tva_root), "publish", record.id]) == 1


def test_fake_publish_is_idempotent(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    assert "publish" not in store.compute_status(tva_root, record.id).stages
    first = publish_session(record.id, root=tva_root, notion=True)
    assert first.status == "ok"
    assert first.created is True
    assert first.page_id
    second = publish_session(record.id, root=tva_root, notion=True)
    assert second.created is False
    assert second.page_id == first.page_id
    fake = FakePublishClient(tva_root)
    matches = [page for page in fake._pages.values() if page.title == "11 Sep 2026 Session Debrief"]
    assert len(matches) == 1
    assert matches[0].properties["Summaries"] == "Reviewed the tape against T01."
    assert matches[0].properties["Tags"] == "Trades Summary"
    runs = fake.find_page("TVA runs")
    assert runs is not None
    assert record.id in runs.body
    assert runs.body.count("publish ok") == 1
    artifact = store.read_json(store.publish_path(tva_root, record.id))
    assert artifact["page_id"] == first.page_id
    assert artifact["title"] == "11 Sep 2026 Session Debrief"
    assert store.compute_status(tva_root, record.id).stages["publish"] == "ok"


def test_cli_publish_notion_fake(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    assert main(["--root", str(tva_root), "publish", record.id, "--notion"]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    assert '"created": true' in out
    assert main(["--root", str(tva_root), "publish", record.id, "--notion"]) == 0
    again = capsys.readouterr().out
    assert '"created": false' in again


def test_default_fake_never_calls_notion(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.delenv(ENV_NOTION_PROVIDER, raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("live Notion must not be called in CI")

    monkeypatch.setattr("httpx.Client.request", boom)
    monkeypatch.setattr("httpx.Client.post", boom)
    result = publish_session(record.id, root=tva_root, notion=True)
    assert result.provider == "fake"


def test_live_without_key_fails(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        publish_session(record.id, root=tva_root, notion=True, provider_name="notion")


JOURNAL_DB = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
PAGE_ID = "11111111111111111111111111111111"
RUNS_ID = "22222222222222222222222222222222"
FOREIGN_PAGE_ID = "33333333333333333333333333333333"
OTHER_DAY_PAGE_ID = "44444444444444444444444444444444"


def _journal_props() -> dict:
    return {
        "properties": {
            "Name": {"type": "title"},
            "Tags": {"type": "multi_select"},
            "Summaries": {"type": "rich_text"},
            "Learning 1": {"type": "rich_text"},
            "Learning 2": {"type": "rich_text"},
            "Learning 3": {"type": "rich_text"},
        }
    }


def _page_payload(page_id: str, title: str, *, database_id: str | None = JOURNAL_DB) -> dict:
    parent = {"database_id": database_id} if database_id else {"type": "workspace"}
    return {
        "object": "page",
        "id": page_id,
        "url": f"https://notion.so/{page_id}",
        "archived": False,
        "parent": parent,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
        },
    }


def test_live_mocked_create_then_update(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    created = {"pages": 0, "patches": 0, "logs": 0}
    run_blocks: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        path = request.url.path
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith("/pages") and request.method == "POST":
            created["pages"] += 1
            body = json.loads(request.content)
            assert body["parent"]["database_id"] == JOURNAL_DB
            assert body["properties"]["Tags"]["multi_select"][0]["name"] == "Trades Summary"
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/pages/{PAGE_ID}") and request.method == "GET":
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/pages/{PAGE_ID}") and request.method == "PATCH":
            created["patches"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "GET":
            return httpx.Response(200, json={"results": list(run_blocks), "has_more": False})
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "POST":
            created["logs"] += 1
            body = json.loads(request.content)
            for child in body.get("children") or []:
                para = child.get("paragraph") or {}
                for item in para.get("rich_text") or []:
                    content = (item.get("text") or {}).get("content") or item.get("plain_text") or ""
                    item["plain_text"] = content
                run_blocks.append(child)
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    first = publish_session(record.id, root=tva_root, notion=True, provider_name="notion", client=http)
    assert first.created is True
    assert first.page_id == PAGE_ID
    second = publish_session(record.id, root=tva_root, notion=True, provider_name="notion", client=http)
    assert second.created is False
    assert second.page_id == PAGE_ID
    assert created["pages"] == 1
    assert created["patches"] == 1
    assert created["logs"] == 1


def test_missing_debrief_fails(tva_root: Path) -> None:
    record = _session(tva_root)
    with pytest.raises(ValueError, match="debrief.json"):
        publish_session(record.id, root=tva_root, notion=True)


def test_publish_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "publish", "../outside", "--notion"]) == 1
    assert main(["--root", str(tva_root), "publish", "foo/bar", "--notion"]) == 1


def test_publish_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "publish" not in ALLOWED_RUN_STAGES


def test_publish_omitted_from_status_until_run(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "publish" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_publish_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="publish", error="publish exploded")
    status = store.compute_status(tva_root, record.id)
    assert "publish" not in status.stages
    assert status.error is None


def test_invalidate_drops_publish_json(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    publish_session(record.id, root=tva_root, notion=True)
    assert store.publish_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.publish_path(tva_root, record.id).is_file()


def test_doctor_publish_fake_ok(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NOTION_PROVIDER, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["notion_provider"].status == "ok"


def test_unknown_provider_fails(tva_root: Path) -> None:
    with pytest.raises(ValueError, match="unknown"):
        get_publish_client("grok", root=tva_root)


def test_vienna_day_label_not_utc_date(tva_root: Path) -> None:
    # 22:30 UTC on 11 Sep is 00:30 Vienna on 12 Sep.
    record = _session(tva_root, "2026-09-11_223000")
    payload = store.read_json(store.session_json_path(tva_root, record.id))
    payload["recording"]["start_wallclock_vienna"] = "2026-09-11T22:30:00+00:00"
    store.write_json(store.session_json_path(tva_root, record.id), payload)
    record = store.load_session(tva_root, record.id)
    _write_debrief(tva_root, record.id)
    assert debrief_title(record) == "12 Sep 2026 Session Debrief"
    result = publish_session(record.id, root=tva_root, notion=True)
    assert result.status == "ok"
    artifact = store.read_json(store.publish_path(tva_root, record.id))
    assert artifact["title"] == "12 Sep 2026 Session Debrief"
    fake = FakePublishClient(tva_root)
    assert fake.find_page("12 Sep 2026 Session Debrief") is not None
    assert fake.find_page("11 Sep 2026 Session Debrief") is None


def test_mismatched_debrief_session_fails_closed(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    payload = store.read_json(store.debrief_json_path(tva_root, record.id))
    payload["session_id"] = "2026-09-10_090000"
    store.write_json(store.debrief_json_path(tva_root, record.id), payload)
    with pytest.raises(ValueError, match="session_id"):
        publish_session(record.id, root=tva_root, notion=True)


def test_stale_publish_json_wrong_session_is_ignored(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    first = publish_session(record.id, root=tva_root, notion=True)
    other = _session(tva_root, "2026-09-12_090000")
    _write_debrief(tva_root, other.id)
    copied = store.read_json(store.publish_path(tva_root, record.id))
    copied["session_id"] = record.id
    copied["title"] = "11 Sep 2026 Session Debrief"
    store.write_json(store.publish_path(tva_root, other.id), copied)
    result = publish_session(other.id, root=tva_root, notion=True)
    assert result.page_id != first.page_id
    fake = FakePublishClient(tva_root)
    assert fake.find_page("11 Sep 2026 Session Debrief") is not None
    assert fake.find_page("12 Sep 2026 Session Debrief") is not None


def test_live_without_journal_db_fails(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.delenv(ENV_JOURNAL_DB, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": [], "has_more": False})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="TVA_NOTION_JOURNAL_DB"):
        publish_session(
            record.id, root=tva_root, notion=True, provider_name="notion", client=http
        )


def test_live_stale_publish_id_does_not_crash(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    store.write_json(
        store.publish_path(tva_root, record.id),
        {
            "schema_version": "1",
            "session_id": record.id,
            "provider": "fake",
            "page_id": "fake-not-a-uuid",
            "page_url": "https://notion.fake/fake-not-a-uuid",
            "title": "11 Sep 2026 Session Debrief",
        },
    )
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"pages": 0, "get_stale": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "fake-not-a-uuid" in path:
            hits["get_stale"] += 1
            return httpx.Response(400, json={"message": "invalid id"})
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith("/pages") and request.method == "POST":
            hits["pages"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    assert result.page_id == PAGE_ID
    assert hits["pages"] == 1
    assert hits["get_stale"] == 0


def test_live_stale_publish_id_wrong_title_is_not_retitled(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    store.write_json(
        store.publish_path(tva_root, record.id),
        {
            "schema_version": "1",
            "session_id": record.id,
            "provider": "notion",
            "page_id": OTHER_DAY_PAGE_ID,
            "page_url": f"https://notion.so/{OTHER_DAY_PAGE_ID}",
            "title": "11 Sep 2026 Session Debrief",
        },
    )
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"pages": 0, "patch_other": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith(f"/pages/{OTHER_DAY_PAGE_ID}") and request.method == "GET":
            return httpx.Response(
                200, json=_page_payload(OTHER_DAY_PAGE_ID, "10 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/pages/{OTHER_DAY_PAGE_ID}") and request.method == "PATCH":
            hits["patch_other"] += 1
            return httpx.Response(
                200, json=_page_payload(OTHER_DAY_PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith("/pages") and request.method == "POST":
            hits["pages"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    assert result.page_id == PAGE_ID
    assert hits["pages"] == 1
    assert hits["patch_other"] == 0


def test_live_does_not_update_foreign_workspace_page(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"pages": 0, "patch_foreign": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        _page_payload(
                            FOREIGN_PAGE_ID,
                            "11 Sep 2026 Session Debrief",
                            database_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                        )
                    ],
                    "has_more": False,
                },
            )
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith(f"/pages/{FOREIGN_PAGE_ID}") and request.method == "PATCH":
            hits["patch_foreign"] += 1
            return httpx.Response(
                200,
                json=_page_payload(
                    FOREIGN_PAGE_ID,
                    "11 Sep 2026 Session Debrief",
                    database_id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                ),
            )
        if path.endswith("/pages") and request.method == "POST":
            hits["pages"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    assert result.page_id == PAGE_ID
    assert hits["pages"] == 1
    assert hits["patch_foreign"] == 0


def test_live_log_dedupes_across_paginated_blocks(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"pages": 0, "logs": 0, "child_pages": 0}

    def _para(text: str) -> dict:
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"plain_text": text}]},
        }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith("/pages") and request.method == "POST":
            hits["pages"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/pages/{PAGE_ID}") and request.method in {"GET", "PATCH"}:
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "GET":
            hits["child_pages"] += 1
            cursor = request.url.params.get("start_cursor")
            if not cursor:
                return httpx.Response(
                    200,
                    json={
                        "results": [_para("noise")] * 100,
                        "has_more": True,
                        "next_cursor": "page-2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        _para(f"2026-09-11 16:01 Vienna | session {record.id} | publish ok")
                    ],
                    "has_more": False,
                },
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "POST":
            hits["logs"] += 1
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    first = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    second = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    assert first.page_id == PAGE_ID
    assert second.page_id == PAGE_ID
    assert hits["pages"] == 1
    assert hits["logs"] == 0
    assert hits["child_pages"] >= 2


def test_live_log_loose_publish_mention_still_appends(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"logs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(200, json=_journal_props())
        if path.endswith("/pages") and request.method == "POST":
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "block",
                            "type": "paragraph",
                            "paragraph": {
                                "rich_text": [
                                    {
                                        "plain_text": (
                                            f"do not publish {record.id} until the debrief exists"
                                        )
                                    }
                                ]
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children") and request.method == "POST":
            hits["logs"] += 1
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    publish_session(record.id, root=tva_root, notion=True, provider_name="notion", client=http)
    assert hits["logs"] == 1


def test_live_title_property_not_name_still_finds(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_JOURNAL_DB, JOURNAL_DB)
    monkeypatch.setenv(ENV_RUNS_PAGE, RUNS_ID)
    hits = {"pages": 0, "patches": 0, "name_query": 0, "session_query": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if "/query" in path:
            body = json.loads(request.content)
            prop = body.get("filter", {}).get("property")
            if prop == "Name":
                hits["name_query"] += 1
                return httpx.Response(400, json={"message": "unknown property"})
            if prop == "Session":
                hits["session_query"] += 1
                return httpx.Response(
                    200,
                    json={
                        "results": [_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")],
                        "has_more": False,
                    },
                )
            return httpx.Response(400, json={"message": prop})
        if path.endswith("/search"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith(f"/databases/{JOURNAL_DB}"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "Session": {"type": "title"},
                        "Tags": {"type": "multi_select"},
                        "Summaries": {"type": "rich_text"},
                        "Learning 1": {"type": "rich_text"},
                        "Learning 2": {"type": "rich_text"},
                        "Learning 3": {"type": "rich_text"},
                    }
                },
            )
        if path.endswith(f"/pages/{PAGE_ID}") and request.method == "PATCH":
            hits["patches"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith("/pages") and request.method == "POST":
            hits["pages"] += 1
            return httpx.Response(
                200, json=_page_payload(PAGE_ID, "11 Sep 2026 Session Debrief")
            )
        if path.endswith(f"/blocks/{RUNS_ID}/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    result = publish_session(
        record.id, root=tva_root, notion=True, provider_name="notion", client=http
    )
    assert result.page_id == PAGE_ID
    assert result.created is False
    assert hits["pages"] == 0
    assert hits["patches"] == 1
    assert hits["session_query"] >= 1
