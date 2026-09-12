from __future__ import annotations

import json
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tradevidanalyser.cli import main
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.serve import create_app, token_required_for_host
from tradevidanalyser import store

EXAMPLE_LATEST = Path(__file__).resolve().parents[1] / "examples" / "api" / "latest.json"


def _keys(payload: dict) -> set[str]:
    return set(payload)


def test_token_required_off_loopback(tva_root: Path) -> None:
    assert token_required_for_host("0.0.0.0")
    assert token_required_for_host("192.168.1.10")
    assert not token_required_for_host("127.0.0.1")
    assert not token_required_for_host("localhost")
    with pytest.raises(ValueError, match="TVA_API_TOKEN"):
        create_app(tva_root, host="0.0.0.0")
    with pytest.raises(ValueError, match="TVA_API_TOKEN"):
        create_app(tva_root, host="192.168.1.10")
    app = create_app(tva_root, host="192.168.1.10", token="secret")
    client = TestClient(app)
    assert client.get("/sessions/latest").status_code == 401
    assert client.get("/health").status_code == 401
    assert client.get("/sessions/latest", headers={"Authorization": "Bearer nope"}).status_code == 401
    missing = client.get("/sessions/latest", headers={"Authorization": "Bearer secret"})
    assert missing.status_code == 404


def test_loopback_allows_missing_token(tva_root: Path) -> None:
    client = TestClient(create_app(tva_root, host="127.0.0.1"))
    assert client.get("/health").status_code == 200


def test_cli_refuses_wildcard_without_token(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("TVA_API_TOKEN", raising=False)

    def fail_run(*args, **kwargs):
        raise AssertionError("uvicorn must not start without a token")

    monkeypatch.setattr("uvicorn.run", fail_run)
    code = main(["--root", str(tva_root), "serve", "--host", "0.0.0.0"])
    assert code == 1
    assert "TVA_API_TOKEN" in capsys.readouterr().out


def test_media_404_when_gated(tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = ingest(sample_video, root=tva_root)
    monkeypatch.delenv("TVA_SERVE_MEDIA", raising=False)
    client = TestClient(create_app(tva_root))
    clips = store.clips_dir(tva_root, record.id)
    clips.mkdir(parents=True, exist_ok=True)
    (clips / "chapter.mp4").write_bytes(b"not-a-real-clip")
    assert client.get(f"/sessions/{record.id}/clips/chapter.mp4").status_code == 404
    monkeypatch.setenv("TVA_SERVE_MEDIA", "1")
    enabled = TestClient(create_app(tva_root))
    assert enabled.get(f"/sessions/{record.id}/clips/missing.mp4").status_code == 404
    ok = enabled.get(f"/sessions/{record.id}/clips/chapter.mp4")
    assert ok.status_code == 200
    assert ok.content == b"not-a-real-clip"
    assert enabled.get(f"/sessions/{record.id}/clips/../session.json").status_code == 404


def test_sessions_days_and_status_filter(tva_root: Path, tmp_path: Path, write_video) -> None:
    today = date.today()
    recent = today.isoformat()
    old = (today - timedelta(days=20)).isoformat()
    ingest(write_video(tmp_path / f"{recent} 14-30-00.mp4"), root=tva_root)
    ingest(write_video(tmp_path / f"{old} 10-00-00.mp4"), root=tva_root)
    client = TestClient(create_app(tva_root))
    all_ids = client.get("/sessions").json()["sessions"]
    assert len(all_ids) == 2
    week = client.get("/sessions", params={"days": 7}).json()["sessions"]
    assert any(sid.startswith(recent) for sid in week)
    assert all(not sid.startswith(old) for sid in week)
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert len(missing) == 2
    ok_only = client.get("/sessions", params={"status": "ok"}).json()["sessions"]
    assert ok_only  # ingest is ok on both


def test_background_run_flips_status(tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    record = ingest(sample_video, root=tva_root)
    release = threading.Event()
    started = threading.Event()
    real = transcribe_session

    def blocked(session_id, *, root, provider_name=None):
        started.set()
        release.wait(timeout=5)
        return real(session_id, root=root, provider_name=provider_name)

    monkeypatch.setattr("tradevidanalyser.serve.transcribe_session", blocked)
    client = TestClient(create_app(tva_root))
    posted = client.post(f"/sessions/{record.id}/run", params={"stages": "transcribe,extract"})
    assert posted.status_code == 202
    assert posted.json()["accepted"] is True
    assert started.wait(timeout=2)
    status = client.get(f"/sessions/{record.id}/status").json()
    assert status["stages"]["transcribe"] == "running"
    release.set()
    deadline = time.time() + 8
    body = status
    while time.time() < deadline:
        body = client.get(f"/sessions/{record.id}/status").json()
        if body["stages"].get("transcribe") == "ok" and body["stages"].get("extract") == "ok":
            break
        time.sleep(0.05)
    assert body["stages"]["transcribe"] == "ok"
    assert body["stages"]["extract"] == "ok"
    assert body.get("error") is None


def test_latest_contract_matches_example_keys(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    expected = json.loads(EXAMPLE_LATEST.read_text(encoding="utf-8"))
    body = TestClient(create_app(tva_root)).get("/sessions/latest").json()
    assert _keys(body) == _keys(expected)
    assert _keys(body["session"]) == _keys(expected["session"])
    assert _keys(body["status"]) == _keys(expected["status"])
    assert _keys(body["transcript"]) == _keys(expected["transcript"])
    assert _keys(body["insights"]) == _keys(expected["insights"])
    openapi = TestClient(create_app(tva_root)).get("/openapi.json").json()
    latest_schema = openapi["paths"]["/sessions/latest"]["get"]["responses"]["200"]
    assert "content" in latest_schema
