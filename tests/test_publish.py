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


def _session(root: Path, session_id: str = "2026-09-11_143000") -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
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


def test_live_mocked_create_then_update(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    _write_debrief(tva_root, record.id)
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    monkeypatch.setenv(ENV_JOURNAL_DB, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")
    created = {"pages": 0, "patches": 0, "logs": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer secret"
        path = request.url.path
        if path.endswith("/search") or "/query" in path:
            return httpx.Response(200, json={"results": [], "has_more": False})
        if path.endswith("/databases/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"):
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "Name": {"type": "title"},
                        "Tags": {"type": "multi_select"},
                        "Summaries": {"type": "rich_text"},
                        "Learning 1": {"type": "rich_text"},
                        "Learning 2": {"type": "rich_text"},
                        "Learning 3": {"type": "rich_text"},
                    }
                },
            )
        if path.endswith("/pages") and request.method == "POST":
            created["pages"] += 1
            body = json.loads(request.content)
            assert body["parent"]["database_id"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            assert body["properties"]["Tags"]["multi_select"][0]["name"] == "Trades Summary"
            return httpx.Response(
                200,
                json={
                    "object": "page",
                    "id": "page-1",
                    "url": "https://notion.so/page-1",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "11 Sep 2026 Session Debrief"}],
                        }
                    },
                },
            )
        if path.endswith("/pages/page-1") and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "object": "page",
                    "id": "page-1",
                    "url": "https://notion.so/page-1",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "11 Sep 2026 Session Debrief"}],
                        }
                    },
                },
            )
        if path.endswith("/pages/page-1") and request.method == "PATCH":
            created["patches"] += 1
            return httpx.Response(
                200,
                json={
                    "object": "page",
                    "id": "page-1",
                    "url": "https://notion.so/page-1",
                    "properties": {
                        "Name": {
                            "type": "title",
                            "title": [{"plain_text": "11 Sep 2026 Session Debrief"}],
                        }
                    },
                },
            )
        if path.endswith("/blocks/page-1/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        if "/blocks/" in path and path.endswith("/children") and request.method == "POST":
            created["logs"] += 1
            return httpx.Response(200, json={"results": []})
        if path.endswith("/blocks/tva-runs/children"):
            created["logs"] += 1
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": path})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    first = publish_session(record.id, root=tva_root, notion=True, provider_name="notion", client=http)
    assert first.created is True
    assert first.page_id == "page-1"
    assert created["pages"] == 1
    second = publish_session(record.id, root=tva_root, notion=True, provider_name="notion", client=http)
    assert second.created is False
    assert second.page_id == "page-1"
    assert created["pages"] == 1
    assert created["patches"] == 1


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
