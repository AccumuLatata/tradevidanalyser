"""Read/write Session Record files under TVA_ROOT."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

from tradevidanalyser import config
from tradevidanalyser.schema import Insights, SessionRecord, SessionStatus, Transcript

_FRAME_JPG = re.compile(r"^\d+\.\d{3}\.jpg$")
CONTACT_SHEET_NAME = "contact_sheet.jpg"

_STATUS_LOCK = threading.Lock()


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


def desktop_audio_path(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "audio" / "desktop.opus"


def clips_dir(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "clips"


def clip_path(root: Path, session_id: str, name: str) -> Path:
    return clips_dir(root, session_id) / name


def frames_dir(root: Path, session_id: str) -> Path:
    return config.session_dir(root, session_id) / "frames"


def frame_filename(t: float) -> str:
    return f"{round(float(t) + 0.0, 3):.3f}.jpg"


def frame_path(root: Path, session_id: str, t: float) -> Path:
    return frames_dir(root, session_id) / frame_filename(t)


def contact_sheet_path(root: Path, session_id: str) -> Path:
    return frames_dir(root, session_id) / CONTACT_SHEET_NAME


def list_frame_jpgs(root: Path, session_id: str) -> list[Path]:
    folder = frames_dir(root, session_id)
    if not folder.is_dir():
        return []
    return sorted(path for path in folder.iterdir() if path.is_file() and _FRAME_JPG.match(path.name))


def invalidate_downstream(root: Path, session_id: str) -> None:
    """Drop transcript/insights so a changed recording is not left looking complete."""
    transcript_path(root, session_id).unlink(missing_ok=True)
    insights_path(root, session_id).unlink(missing_ok=True)


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


def compute_status(
    root: Path,
    session_id: str,
    *,
    error: str | None = None,
    cost_usd: float | None = None,
    running: list[str] | None = None,
    failed: str | None = None,
) -> SessionStatus:
    with _STATUS_LOCK:
        return _compute_status_locked(
            root,
            session_id,
            error=error,
            cost_usd=cost_usd,
            running=running,
            failed=failed,
        )


def _compute_status_locked(
    root: Path,
    session_id: str,
    *,
    error: str | None,
    cost_usd: float | None,
    running: list[str] | None,
    failed: str | None,
) -> SessionStatus:
    stages: dict[str, str] = {}
    stages["ingest"] = "ok" if session_json_path(root, session_id).is_file() else "missing"
    stages["transcribe"] = "ok" if transcript_path(root, session_id).is_file() else "missing"
    stages["extract"] = "ok" if insights_path(root, session_id).is_file() else "missing"
    stages["frames"] = "ok" if list_frame_jpgs(root, session_id) else "missing"
    path = status_path(root, session_id)
    previous: SessionStatus | None = None
    if path.is_file():
        try:
            previous = SessionStatus.model_validate(read_json(path))
        except (ValueError, OSError):
            previous = None
    if cost_usd is None and previous is not None:
        cost_usd = previous.cost_usd
    if previous is not None:
        for name, state in previous.stages.items():
            if state == "failed" and stages.get(name) == "missing":
                stages[name] = "failed"
    if failed and stages.get(failed) != "ok":
        stages[failed] = "failed"
    if error is None and previous is not None:
        error = previous.error
    if not any(state == "failed" for state in stages.values()):
        error = None
    durable = SessionStatus(session_id=session_id, stages=stages, error=error, cost_usd=cost_usd)  # type: ignore[arg-type]
    write_json(path, durable.model_dump(mode="json"))
    if not running:
        return durable
    view = dict(stages)
    for name in running:
        if view.get(name) != "ok":
            view[name] = "running"
    view_error = None if not any(state == "failed" for state in view.values()) else error
    return SessionStatus(session_id=session_id, stages=view, error=view_error, cost_usd=cost_usd)  # type: ignore[arg-type]


def list_session_ids(root: Path) -> list[str]:
    base = config.sessions_dir(root)
    if not base.is_dir():
        return []
    ids = [p.name for p in base.iterdir() if p.is_dir() and (p / "session.json").is_file()]
    return sorted(ids)


def latest_session_id(root: Path) -> str | None:
    ids = list_session_ids(root)
    return ids[-1] if ids else None
