from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.context import (
    ENV_JOURNAL_DB,
    ENV_NOTION_KEY,
    ENV_NOTION_PROVIDER,
    FakeNotionClient,
    LiveNotionClient,
    NotionPage,
    brief_day_label,
    build_context,
    context_session,
    drc_title,
    get_notion_client,
    macro_brief_title,
    ny_brief_title,
    session_calendar_date,
)
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, fills_session, transcribe_session
from tradevidanalyser.schema import RecordingInfo, SessionRecord
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

def _session(
    root: Path,
    session_id: str = "2026-09-11_143000",
    *,
    start: str = "2026-09-11T14:30:00+02:00",
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=start,
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_table(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = list(rows[0])
    pq.write_table(pa.table({name: [row.get(name) for row in rows] for name in names}), path)


def _write_trades(root: Path, session_id: str, rows: list[dict]) -> None:
    _write_table(store.trades_path(root, session_id), rows)


def _notion_pages() -> list[NotionPage]:
    return [
        NotionPage(
            title="11 Sep 2026 Macro Brief",
            url="https://notion.so/macro",
            properties={"Bias NQ": "long ONH", "Conviction": "medium"},
        ),
        NotionPage(
            title="11 Sep 2026 NY session Brief",
            url="https://notion.so/ny",
            properties={
                "Bias NQ": "long ONH",
                "Bias ES": "neutral",
                "Conviction": "high",
                "Kill levels": "18480 / 18620",
            },
        ),
        NotionPage(
            title="DRC 11092026",
            url="https://notion.so/drc",
            properties={"A": "2", "B": "1", "C": "0"},
        ),
    ]


def test_brief_day_titles_are_english_and_unpadded() -> None:
    day = date(2026, 9, 11)
    assert brief_day_label(day) == "11 Sep 2026"
    assert macro_brief_title(day) == "11 Sep 2026 Macro Brief"
    assert ny_brief_title(day) == "11 Sep 2026 NY session Brief"
    assert drc_title(day) == "DRC 11092026"
    assert brief_day_label(date(2026, 3, 1)) == "1 Mar 2026"


def test_missing_brief_is_null_plus_gap(tva_root: Path) -> None:
    record = _session(tva_root)
    result = context_session(record.id, root=tva_root, notion=FakeNotionClient())
    assert result.status == "ok"
    payload = store.read_json(store.context_path(tva_root, record.id))
    assert payload["brief"] is None
    assert payload["drc"] is None
    assert payload["lab"] is None
    assert any("brief not found" in gap for gap in payload["gaps"])
    assert "lab-dir not set" in payload["gaps"]
    assert store.compute_status(tva_root, record.id).stages["context"] == "ok"


def test_quoted_brief_and_drc_from_fake_notion(tva_root: Path) -> None:
    record = _session(tva_root)
    report = build_context(record, root=tva_root, notion=FakeNotionClient(_notion_pages()))
    assert report.brief is not None
    assert report.brief.quoted is True
    assert report.brief.macro_url == "https://notion.so/macro"
    assert report.brief.ny_url == "https://notion.so/ny"
    assert report.brief.bias_nq == "long ONH"
    assert report.brief.bias_es == "neutral"
    assert report.brief.conviction == "high"
    assert report.brief.kill_levels == "18480 / 18620"
    assert report.drc is not None
    assert report.drc.url == "https://notion.so/drc"
    assert report.drc.scores["A"] == "2"


def test_lab_join_by_entry_fill_id(tva_root: Path, tmp_path: Path) -> None:
    record = _session(tva_root)
    _write_trades(
        tva_root,
        record.id,
        [
            {"tva_trade_id": "T01", "trade_id": "jt-1", "entry_fill_id": "fill-a"},
            {"tva_trade_id": "T02", "trade_id": "jt-2", "entry_fill_id": "fill-b"},
        ],
    )
    lab = tmp_path / "journal"
    lab.mkdir()
    _write_table(
        lab / "journal_attribution.parquet",
        [
            {
                "trade_id": "jt-1",
                "entry_fill_id": "fill-a",
                "nearest_level_token": "pdPOC",
                "level_context": "at_level",
                "tag_alignment": "all_aligned",
            }
        ],
    )
    _write_table(
        lab / "journal_zones.parquet",
        [{"trade_id": "jt-1", "entry_fill_id": "fill-a", "zone_id": "z-onh"}],
    )
    _write_table(
        lab / "triggers.parquet",
        [{"trade_id": "fill-a", "inferred_triggers_1m": "3c,hold"}],
    )
    report = build_context(
        record,
        root=tva_root,
        notion=FakeNotionClient(_notion_pages()),
        lab_dir=lab,
    )
    assert report.lab is not None
    first = report.lab.per_trade["T01"]
    assert first.nearest_level_token == "pdPOC"
    assert first.level_context == "at_level"
    assert first.tag_alignment == "all_aligned"
    assert first.zone_id == "z-onh"
    assert first.inferred_triggers_1m == "3c,hold"
    second = report.lab.per_trade["T02"]
    assert second.nearest_level_token is None
    assert any("no lab row for T02" in gap for gap in report.gaps)


def test_no_recompute_in_context_source() -> None:
    src = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "context.py"
    text = src.read_text(encoding="utf-8")
    assert "thesistester" not in text
    assert "compute_all_levels" not in text
    assert "simulate_trades" not in text
    assert "attribute_files" not in text


def test_lab_dir_missing_is_fail_closed(tva_root: Path, tmp_path: Path) -> None:
    record = _session(tva_root)
    with pytest.raises(ValueError, match="lab-dir"):
        build_context(record, root=tva_root, lab_dir=tmp_path / "missing")


def test_cli_context_writes_facts_and_is_byte_identical(
    tva_root: Path, tmp_path: Path, capsys
) -> None:
    record = _session(tva_root)
    _write_trades(
        tva_root,
        record.id,
        [{"tva_trade_id": "T01", "trade_id": "jt-1", "entry_fill_id": "fill-a"}],
    )
    lab = tmp_path / "lab"
    lab.mkdir()
    _write_table(
        lab / "attribution.parquet",
        [
            {
                "entry_fill_id": "fill-a",
                "nearest_level_token": "ONH",
                "level_context": "between_levels",
                "tag_alignment": "partial",
            }
        ],
    )
    assert "context" not in store.compute_status(tva_root, record.id).stages
    assert main(["--root", str(tva_root), "context", record.id, "--lab-dir", str(lab)]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    first = store.context_path(tva_root, record.id).read_text(encoding="utf-8")
    payload = store.read_json(store.context_path(tva_root, record.id))
    assert payload["brief"] is None
    assert payload["lab"]["per_trade"]["T01"]["nearest_level_token"] == "ONH"
    assert main(["--root", str(tva_root), "context", record.id, "--lab-dir", str(lab)]) == 0
    assert store.context_path(tva_root, record.id).read_text(encoding="utf-8") == first


def test_context_omitted_from_status_until_run(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "context" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_context_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="context", error="context exploded")
    status = store.compute_status(tva_root, record.id)
    assert "context" not in status.stages
    assert status.error is None


def test_context_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "context", "../outside"]) == 1
    assert main(["--root", str(tva_root), "context", "foo/bar"]) == 1


def test_context_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "context" not in ALLOWED_RUN_STAGES


def test_invalidate_downstream_drops_context(tva_root: Path) -> None:
    record = _session(tva_root)
    context_session(record.id, root=tva_root, notion=FakeNotionClient())
    assert store.context_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.context_path(tva_root, record.id).is_file()
    assert "context" not in store.compute_status(tva_root, record.id).stages


def test_fills_skip_drops_stale_context(tva_root: Path) -> None:
    record = _session(tva_root)
    context_session(record.id, root=tva_root, notion=FakeNotionClient())
    assert store.context_path(tva_root, record.id).is_file()
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "skipped"
    assert not store.context_path(tva_root, record.id).is_file()


def test_default_fake_never_calls_live_notion(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.delenv(ENV_NOTION_PROVIDER, raising=False)

    def boom(*_args, **_kwargs):
        raise AssertionError("live Notion must not be called in CI")

    monkeypatch.setattr("httpx.Client.post", boom)
    monkeypatch.setattr("httpx.Client.get", boom)
    report = build_context(record, root=tva_root)
    assert report.brief is None
    assert any("brief not found" in gap for gap in report.gaps)


def test_live_client_uses_mocked_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        assert request.headers["authorization"] == "Bearer secret"
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "page",
                            "id": "page-1",
                            "url": "https://notion.so/ny",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [{"plain_text": "11 Sep 2026 NY session Brief"}],
                                },
                                "Bias NQ": {
                                    "type": "rich_text",
                                    "rich_text": [{"plain_text": "short dVWAP"}],
                                },
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": "no"})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    notion = get_notion_client("notion", client=client)
    assert isinstance(notion, LiveNotionClient)
    page = notion.find_page("11 Sep 2026 NY session Brief")
    assert page is not None
    assert page.properties["Bias NQ"] == "short dVWAP"
    assert calls
    assert all(path.startswith("GET /v1/") or path.startswith("POST /v1/") for path in calls)


def test_live_without_key_does_not_construct_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        get_notion_client("notion")


def test_doctor_notion_key_warns(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.delenv(ENV_NOTION_PROVIDER, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["notion_key"].status == "warn"
    assert ids["notion_provider"].status == "ok"


def test_doctor_live_notion_without_key_fails(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    report = run_doctor(tva_root)
    ids = {item.id: item for item in report.checks}
    assert ids["notion_key"].status == "warn"
    assert ids["notion_provider"].status == "fail"
    assert not report.ok


def test_utc_wallclock_uses_vienna_calendar_date() -> None:
    # 22:30 UTC on 11 Sep is 00:30 Vienna on 12 Sep.
    record = SessionRecord(
        id="2026-09-11_223000",
        recording=RecordingInfo(
            path="recordings/2026-09-11 22-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T22:30:00+00:00",
            duration_s=3600.0,
            filename="2026-09-11 22-30-00.mp4",
        ),
    )
    assert session_calendar_date(record) == date(2026, 9, 12)
    naive = record.model_copy(
        update={
            "recording": record.recording.model_copy(
                update={"start_wallclock_vienna": "2026-09-12T00:30:00"}
            )
        }
    )
    assert session_calendar_date(naive) == date(2026, 9, 12)


def test_utc_wallclock_selects_vienna_day_brief(tva_root: Path) -> None:
    record = _session(
        tva_root,
        "2026-09-11_223000",
        start="2026-09-11T22:30:00+00:00",
    )
    pages = [
        NotionPage(title="11 Sep 2026 NY session Brief", url="https://notion.so/wrong"),
        NotionPage(
            title="12 Sep 2026 NY session Brief",
            url="https://notion.so/ny",
            properties={"Bias NQ": "long ONH"},
        ),
    ]
    report = build_context(record, root=tva_root, notion=FakeNotionClient(pages))
    assert report.brief is not None
    assert report.brief.ny_url == "https://notion.so/ny"
    assert report.brief.bias_nq == "long ONH"


def test_lab_join_prefers_entry_fill_id_over_trade_id(
    tva_root: Path, tmp_path: Path
) -> None:
    record = _session(tva_root)
    _write_trades(
        tva_root,
        record.id,
        [{"tva_trade_id": "T01", "trade_id": "jt-1", "entry_fill_id": "fill-a"}],
    )
    lab = tmp_path / "journal"
    lab.mkdir()
    _write_table(
        lab / "attribution.parquet",
        [
            {
                "trade_id": "jt-1",
                "entry_fill_id": "fill-other",
                "nearest_level_token": "WRONG",
            },
            {
                "trade_id": "fill-a",
                "nearest_level_token": "ALSO-WRONG",
            },
            {
                "trade_id": "jt-9",
                "entry_fill_id": "fill-a",
                "nearest_level_token": "pdPOC",
            },
        ],
    )
    report = build_context(
        record,
        root=tva_root,
        notion=FakeNotionClient(),
        lab_dir=lab,
    )
    assert report.lab is not None
    assert report.lab.per_trade["T01"].nearest_level_token == "pdPOC"


def test_lab_join_skips_conflicting_fill_on_trade_id(
    tva_root: Path, tmp_path: Path
) -> None:
    record = _session(tva_root)
    _write_trades(
        tva_root,
        record.id,
        [{"tva_trade_id": "T01", "trade_id": "jt-1", "entry_fill_id": "fill-a"}],
    )
    lab = tmp_path / "journal"
    lab.mkdir()
    _write_table(
        lab / "attribution.parquet",
        [
            {
                "trade_id": "jt-1",
                "entry_fill_id": "fill-other",
                "nearest_level_token": "WRONG",
            }
        ],
    )
    report = build_context(
        record,
        root=tva_root,
        notion=FakeNotionClient(),
        lab_dir=lab,
    )
    assert report.lab is not None
    assert report.lab.per_trade["T01"].nearest_level_token is None
    assert any("no lab row for T01" in gap for gap in report.gaps)


def test_live_without_key_fails_build_context(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session(tva_root)
    monkeypatch.delenv(ENV_NOTION_KEY, raising=False)
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    with pytest.raises(ValueError, match="NOTION_API_KEY"):
        build_context(record, root=tva_root, notion_provider="notion")
    assert not store.context_path(tva_root, record.id).is_file()


def test_live_search_paginates_past_first_page(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/search":
            body = request.read()
            text = body.decode("utf-8")
            if "start_cursor" not in text:
                return httpx.Response(
                    200,
                    json={
                        "results": [
                            {
                                "object": "page",
                                "id": "page-0",
                                "url": "https://notion.so/other",
                                "properties": {
                                    "Name": {
                                        "type": "title",
                                        "title": [{"plain_text": "10 Sep 2026 NY session Brief"}],
                                    }
                                },
                            }
                        ],
                        "has_more": True,
                        "next_cursor": "cur-2",
                    },
                )
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "page",
                            "id": "page-1",
                            "url": "https://notion.so/ny",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [
                                        {"plain_text": "11 Sep 2026 NY session Brief  "}
                                    ],
                                },
                                "Bias NQ": {
                                    "type": "status",
                                    "status": {"name": "short dVWAP"},
                                },
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json={"results": [], "has_more": False})
        return httpx.Response(404, json={"message": "no"})

    notion = get_notion_client("notion", client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = notion.find_page("11 Sep 2026 NY session Brief")
    assert page is not None
    assert page.properties["Bias NQ"] == "short dVWAP"
    assert calls.count("POST /v1/search") >= 2


def test_live_db_error_falls_back_to_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    monkeypatch.setenv(ENV_JOURNAL_DB, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa")

    def handler(request: httpx.Request) -> httpx.Response:
        if "/databases/" in request.url.path:
            return httpx.Response(400, json={"message": "unknown property"})
        if request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "object": "page",
                            "id": "page-1",
                            "url": "https://notion.so/ny",
                            "properties": {
                                "Name": {
                                    "type": "title",
                                    "title": [{"plain_text": "11 Sep 2026 NY session Brief"}],
                                }
                            },
                        }
                    ]
                },
            )
        if request.url.path.endswith("/children"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(404, json={"message": "no"})

    notion = get_notion_client("notion", client=httpx.Client(transport=httpx.MockTransport(handler)))
    page = notion.find_page("11 Sep 2026 NY session Brief")
    assert page is not None
    assert page.url == "https://notion.so/ny"


def test_invalid_journal_db_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_NOTION_KEY, "secret")
    monkeypatch.setenv(ENV_NOTION_PROVIDER, "notion")
    monkeypatch.setenv(ENV_JOURNAL_DB, "../not-an-id")
    with pytest.raises(ValueError, match="TVA_NOTION_JOURNAL_DB"):
        get_notion_client("notion")


@pytest.mark.golden
def test_golden_context_has_bias_or_skips() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    paths = list(golden.rglob("context.json"))
    if not paths:
        pytest.skip("no golden context.json")
    for path in paths:
        payload = store.read_json(path)
        brief = payload.get("brief")
        if brief:
            assert brief.get("quoted") is True
            assert brief.get("bias_nq") or brief.get("bias_es")
