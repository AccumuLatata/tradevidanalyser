"""Read/write Session Record files under TVA_ROOT."""

from __future__ import annotations

import json
from pathlib import Path

from tradevidanalyser import config
from tradevidanalyser.schema import Insights, SessionRecord, SessionStatus, Transcript


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def session_json_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "session.json"


def transcript_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "transcript.json"


def insights_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "insights.json"


def status_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "status.json"


def audio_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "audio" / "mic.opus"


def load_session(root: Path, session_id: str) -> SessionRecord:
    return SessionRecord.model_validate(read_json(session_json_path(root, session_id)))


def save_session(root: Path, record: SessionRecord) -> Path:
    path = session_json_path(root, record.id)
    write_json(path, record.model_dump(mode="json"))
    return path


def load_transcript(root: Path, session_id: str) -> Transcript:
    return Transcript.model_validate(read_json(transcript_path(root, session_id)))


def save_transcript(root: Path, session_id: str, transcript: Transcript) -> Path:
    path = transcript_path(root, session_id)
    write_json(path, transcript.model_dump(mode="json"))
    return path


def load_insights(root: Path, session_id: str) -> Insights:
    return Insights.model_validate(read_json(insights_path(root, session_id)))


def save_insights(root: Path, session_id: str, insights: Insights) -> Path:
    path = insights_path(root, session_id)
    write_json(path, insights.model_dump(mode="json"))
    return path


def compute_status(root: Path, session_id: str, *, error: str | None = None) -> SessionStatus:
    stages: dict[str, str] = {}
    stages["ingest"] = "ok" if session_json_path(root, session_id).is_file() else "missing"
    stages["transcribe"] = "ok" if transcript_path(root, session_id).is_file() else "missing"
    stages["extract"] = "ok" if insights_path(root, session_id).is_file() else "missing"
    status = SessionStatus(session_id=session_id, stages=stages, error=error)  # type: ignore[arg-type]
    write_json(status_path(root, session_id), status.model_dump(mode="json"))
    return status


def list_session_ids(root: Path) -> list[str]:
    base = config.sessions_dir(root)
    if not base.is_dir():
        return []
    ids = [p.name for p in base.iterdir() if p.is_dir() and (p / "session.json").is_file()]
    return sorted(ids)


def latest_session_id(root: Path) -> str | None:
    ids = list_session_ids(root)
    return ids[-1] if ids else None
