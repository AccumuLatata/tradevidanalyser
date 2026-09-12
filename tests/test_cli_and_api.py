from pathlib import Path

from fastapi.testclient import TestClient

from tradevidanalyser.cli import main
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.serve import create_app


def test_doctor_ok_when_ffmpeg_present(tva_root: Path) -> None:
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["ffprobe"].status == "ok"
    assert ids["ffmpeg"].status == "ok"
    assert ids["tva_root"].status == "ok"
    assert ids["whisperx"].status in {"ok", "warn"}
    assert ids["cuda"].status in {"ok", "warn"}
    assert ids["deepgram_key"].status in {"ok", "warn"}
    assert ids["elevenlabs_key"].status in {"ok", "warn"}
    assert report.ok


def test_cli_doctor_json(tva_root: Path, capsys) -> None:
    code = main(["--root", str(tva_root), "doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert '"ok"' in out
    assert "ffprobe" in out


def test_cli_ingest_and_status(tva_root: Path, sample_video: Path, capsys) -> None:
    assert main(["--root", str(tva_root), "ingest", str(sample_video)]) == 0
    capsys.readouterr()
    assert main(["--root", str(tva_root), "status"]) == 0
    out = capsys.readouterr().out
    assert "2026-09-11_143000" in out


def test_api_latest_bundle(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    client = TestClient(create_app(tva_root))
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    listing = client.get("/sessions")
    assert listing.json()["latest"] == record.id
    latest = client.get("/sessions/latest")
    assert latest.status_code == 200
    body = latest.json()
    assert body["insights"]["stated_levels"]
    quotes = client.get(f"/sessions/{record.id}/insights")
    assert quotes.status_code == 200
    missing = client.get("/sessions/nope")
    assert missing.status_code == 404
