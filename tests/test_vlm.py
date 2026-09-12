from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.frames import extract_frames
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ocr import OcrRow, write_ocr_parquet
from tradevidanalyser.pipeline import extract_session, transcribe_session, vlm_session
from tradevidanalyser.providers.vlm import (
    DEFAULT_GROK_MODEL,
    ENV_VLM_PROVIDER,
    GEMINI_FILES_URL,
    GEMINI_GENERATE_URL,
    XAI_CHAT_URL,
    FakeVlmProvider,
    GeminiVlmProvider,
    GrokVlmProvider,
    VlmContext,
    VlmError,
    apply_visual_note_guard,
    get_vlm_provider,
)
from tradevidanalyser.schema import SessionRecord, VisualNote
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

from conftest import make_test_video


def _session_with_frames(tva_root: Path, *, seconds: float = 8.0) -> SessionRecord:
    video = make_test_video(
        tva_root / "recordings" / "2026-09-11 14-30-00.mp4",
        seconds=seconds,
        chapters=[(5.0, "ch1")],
    )
    record = ingest(video, root=tva_root)
    extract_frames(record, [5.0], root=tva_root)
    return record


def _write_ocr(root: Path, session_id: str, text: str = "21500") -> None:
    write_ocr_parquet(
        store.ocr_path(root, session_id),
        [
            OcrRow(t=5.0, roi="pnl", text=text, confidence=0.9, parsed=text),
        ],
    )


def _vlm_response(notes: list[dict], *, usage: dict | None = None) -> dict:
    body: dict = {
        "choices": [{"message": {"content": json.dumps({"notes": notes}, ensure_ascii=False)}}]
    }
    if usage:
        body["usage"] = usage
    return body


def test_vlm_off_unless_env_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VLM_PROVIDER, raising=False)
    assert get_vlm_provider() is None
    assert get_vlm_provider("off") is None
    assert get_vlm_provider("fake").name == "fake"
    assert get_vlm_provider("grok").name == "grok"
    assert get_vlm_provider("gemini").name == "gemini"
    with pytest.raises(VlmError, match="unknown"):
        get_vlm_provider("nope")


def test_cli_vlm_skips_when_unset(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv(ENV_VLM_PROVIDER, raising=False)
    record = _session_with_frames(tva_root)
    assert main(["--root", str(tva_root), "vlm", record.id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "skipped"
    assert "TVA_VLM_PROVIDER unset" in out["reason"]
    assert "vlm" not in store.compute_status(tva_root, record.id).stages
    assert not store.visual_notes_path(tva_root, record.id).is_file()


def test_fake_writes_notes_and_status(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv(ENV_VLM_PROVIDER, "fake")
    record = _session_with_frames(tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert main(["--root", str(tva_root), "vlm", record.id]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["provider"] == "fake"
    assert out["notes"]
    assert out["notes"][0]["frames_cited"] == ["5.000"]
    assert store.visual_notes_path(tva_root, record.id).is_file()
    insights = store.load_insights(tva_root, record.id)
    assert insights.visual_notes
    assert insights.visual_notes[0].frames_cited == ["5.000"]
    assert "21500" not in insights.visual_notes[0].text
    status = store.compute_status(tva_root, record.id)
    assert status.stages["vlm"] == "ok"
    assert status.cost_usd is None


def test_guard_drops_number_absent_from_ocr() -> None:
    notes = [VisualNote(text="print is 21500 on the DOM", frames_cited=["5.000"])]
    kept, gaps = apply_visual_note_guard(
        notes,
        ocr_rows=[],
        frame_stems={"5.000"},
        clip_names=set(),
    )
    assert kept == []
    assert any("21500" in gap for gap in gaps)


def test_guard_keeps_number_present_in_ocr(tva_root: Path) -> None:
    record = _session_with_frames(tva_root)
    _write_ocr(tva_root, record.id, "21500")
    from tradevidanalyser.ocr import read_ocr_parquet

    rows = read_ocr_parquet(store.ocr_path(tva_root, record.id))
    notes = [VisualNote(text="OCR printed 21500", t=5.0, frames_cited=["5.000"])]
    kept, gaps = apply_visual_note_guard(
        notes,
        ocr_rows=rows,
        frame_stems={"5.000"},
        clip_names=set(),
    )
    assert len(kept) == 1
    assert kept[0].text == "OCR printed 21500"
    assert gaps == []


def test_pipeline_guard_drops_fake_injected_price(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session_with_frames(tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    planted = [
        VisualNote(text="price printed 21500", frames_cited=["5.000"]),
        VisualNote(text="DOM is visible in the left pane.", frames_cited=["5.000"]),
    ]
    monkeypatch.setattr(
        "tradevidanalyser.pipeline.get_vlm_provider",
        lambda name=None: FakeVlmProvider(notes=planted),
    )
    payload = vlm_session(record.id, root=tva_root, provider_name="fake")
    assert len(payload["notes"]) == 1
    assert "21500" not in payload["notes"][0]["text"]
    assert any("21500" in gap for gap in payload["gaps"])
    insights = store.load_insights(tva_root, record.id)
    assert len(insights.visual_notes) == 1
    assert any("21500" in gap for gap in insights.gaps)


def test_grok_mocked_sends_image_url_not_video(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session_with_frames(tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        notes = [
            {
                "text": "DOM is visible in the left pane.",
                "t": 5.0,
                "frames_cited": ["5.000"],
                "clip": None,
            }
        ]
        return httpx.Response(
            200,
            json=_vlm_response(
                notes,
                usage={"prompt_tokens": 800_000, "completion_tokens": 400},
            ),
        )

    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    provider = GrokVlmProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(
        "tradevidanalyser.pipeline.get_vlm_provider", lambda name=None: provider
    )
    payload = vlm_session(record.id, root=tva_root, provider_name="grok")
    assert captured["url"] == XAI_CHAT_URL
    body = captured["body"]
    assert body["model"] == DEFAULT_GROK_MODEL
    user = body["messages"][1]["content"]
    types = [part.get("type") for part in user]
    assert "image_url" in types
    assert "video_url" not in types
    blob = json.dumps(body)
    assert Path(record.recording.path).name not in blob
    assert "recordings/" not in blob
    assert payload["notes"][0]["frames_cited"] == ["5.000"]
    status = store.compute_status(tva_root, record.id)
    assert status.cost_usd is not None
    assert status.cost_usd > 0
    assert status.stages["vlm"] == "ok"


def test_gemini_mocked_frames_inline(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session_with_frames(tva_root)
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "notes": [
                                                {
                                                    "text": "Chart pane is on the right.",
                                                    "t": 5.0,
                                                    "frames_cited": ["5.000"],
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setenv("GEMINI_API_KEY", "gem-test-key")
    provider = GeminiVlmProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    ctx = VlmContext(
        session_id=record.id,
        frames=store.list_frame_jpgs(tva_root, record.id),
        clips=[],
        ocr_rows=[],
        recording_path=record.recording.path,
    )
    result = provider.annotate(ctx)
    assert GEMINI_GENERATE_URL.split("{")[0] in captured["url"]
    parts = captured["body"]["contents"][0]["parts"]
    assert any("inline_data" in part for part in parts)
    blob = json.dumps(captured["body"])
    assert Path(record.recording.path).name not in blob
    assert result.notes[0].frames_cited == ["5.000"]


def test_gemini_mocked_clip_uses_file_api(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = _session_with_frames(tva_root)
    clips = store.clips_dir(tva_root, record.id)
    clips.mkdir(parents=True, exist_ok=True)
    clip = clips / "5.000.mp4"
    clip.write_bytes(b"\x00\x00fake-mp4")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url).startswith(GEMINI_FILES_URL):
            return httpx.Response(
                200,
                json={"file": {"uri": "https://generativelanguage.googleapis.com/v1beta/files/abc"}},
            )
        body = json.loads(request.content)
        assert "file_data" in json.dumps(body)
        assert Path(record.recording.path).name not in json.dumps(body)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "notes": [
                                                {
                                                    "text": "Redacted chapter clip shows the desk layout.",
                                                    "clip": "5.000.mp4",
                                                }
                                            ]
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            },
        )

    monkeypatch.setenv("GEMINI_API_KEY", "gem-test-key")
    provider = GeminiVlmProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    ctx = VlmContext(
        session_id=record.id,
        frames=[],
        clips=[clip],
        ocr_rows=[],
        recording_path=record.recording.path,
    )
    result = provider.annotate(ctx)
    assert any(url.startswith(GEMINI_FILES_URL) for url in seen)
    assert result.clip == "5.000.mp4"
    assert result.notes[0].clip == "5.000.mp4"


def test_vlm_omitted_from_status_when_not_run(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    status = store.compute_status(tva_root, record.id)
    assert "vlm" not in status.stages
    assert "frames" not in status.stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_vlm_failed_is_omitted(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="vlm", error="vlm exploded")
    status = store.compute_status(tva_root, record.id)
    assert "vlm" not in status.stages
    assert status.error is None
    client = TestClient(create_app(tva_root))
    failed = client.get("/sessions", params={"status": "failed"}).json()["sessions"]
    assert record.id not in failed
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_vlm_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "vlm", "../outside"]) == 1
    assert main(["--root", str(tva_root), "vlm", "foo/bar"]) == 1


def test_extract_preserves_visual_notes(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VLM_PROVIDER, "fake")
    record = _session_with_frames(tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    vlm_session(record.id, root=tva_root)
    before = store.load_insights(tva_root, record.id).visual_notes
    assert before
    extract_session(record.id, root=tva_root)
    after = store.load_insights(tva_root, record.id).visual_notes
    assert after == before


def test_vlm_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "vlm" not in ALLOWED_RUN_STAGES


def test_doctor_vlm_grok_without_key(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VLM_PROVIDER, "grok")
    monkeypatch.setenv("XAI_API_KEY", "   ")
    ids = {c.id: c for c in run_doctor(tva_root).checks}
    assert ids["vlm_provider"].status == "fail"
    assert ids["xai_key"].status == "warn"


def test_doctor_vlm_gemini_without_key(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_VLM_PROVIDER, "gemini")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    ids = {c.id: c for c in run_doctor(tva_root).checks}
    assert ids["vlm_provider"].status == "fail"
    assert ids["gemini_key"].status == "fail"


def test_doctor_vlm_off_is_ok(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_VLM_PROVIDER, raising=False)
    ids = {c.id: c for c in run_doctor(tva_root).checks}
    assert ids["vlm_provider"].status == "ok"
    assert ids["gemini_key"].status == "warn"
