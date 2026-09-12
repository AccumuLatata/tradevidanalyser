"""Read-only HTTP API over TVA_ROOT. Bind to localhost or a Tailscale interface."""

from __future__ import annotations

import hmac
import json
import os
import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from tradevidanalyser import __version__, store
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.schema import Insights, SessionRecord, SessionStatus, Transcript

ENV_TOKEN = "TVA_API_TOKEN"
ENV_SERVE_MEDIA = "TVA_SERVE_MEDIA"
ALLOWED_RUN_STAGES = ("transcribe", "extract")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SESSION_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})_")
_EXAMPLES_DIR = Path(__file__).resolve().parents[2] / "examples" / "api"


def is_loopback_host(host: str) -> bool:
    return host.strip().lower().strip("[]") in LOOPBACK_HOSTS


def token_required_for_host(host: str) -> bool:
    return not is_loopback_host(host)


def serve_media_enabled() -> bool:
    return (os.environ.get(ENV_SERVE_MEDIA) or "").strip().lower() in {"1", "true", "yes", "on"}


def _load_example(name: str) -> dict[str, Any]:
    path = _EXAMPLES_DIR / name
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


class SessionListResponse(BaseModel):
    sessions: list[str]
    latest: str | None = None

    model_config = ConfigDict(
        json_schema_extra={"examples": [_load_example("sessions.json") or {"sessions": [], "latest": None}]}
    )


class SessionBundle(BaseModel):
    session: SessionRecord
    status: SessionStatus
    transcript: Transcript | None = None
    insights: Insights | None = None

    model_config = ConfigDict(json_schema_extra={"examples": [_load_example("latest.json")]})


class RunAccepted(BaseModel):
    accepted: bool = True
    session_id: str
    stages: list[str]
    status: SessionStatus


def create_app(
    root: Path, *, host: str = "127.0.0.1", token: str | None = None
) -> FastAPI:
    resolved_token = (token if token is not None else os.environ.get(ENV_TOKEN) or "").strip()
    require_auth = token_required_for_host(host)
    if require_auth and not resolved_token:
        raise ValueError(
            "TVA_API_TOKEN is required when binding off-loopback (including 0.0.0.0)"
        )

    app = FastAPI(
        title="TradeVidAnalyser",
        version=__version__,
        description="Headless Session Record API for Grok bots.",
    )
    app.state.root = root.resolve()
    app.state.token = resolved_token
    app.state.require_auth = require_auth
    app.state.jobs_lock = threading.Lock()
    app.state.jobs: dict[str, set[str]] = {}

    @app.middleware("http")
    async def _bearer_auth(request: Request, call_next):  # type: ignore[no-untyped-def]
        if app.state.require_auth:
            header = request.headers.get("authorization") or ""
            scheme, _, supplied = header.partition(" ")
            expected = app.state.token
            same_len = len(supplied) == len(expected)
            matched = same_len and hmac.compare_digest(supplied, expected)
            if scheme.lower() != "bearer" or not matched:
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    def health() -> JSONResponse:
        report = run_doctor(app.state.root)
        return JSONResponse(report.model_dump(mode="json"))

    @app.get("/sessions", response_model=SessionListResponse)
    def sessions(
        days: int | None = Query(default=None, ge=0, description="Keep sessions from the last N calendar days"),
        status: str | None = Query(default=None, description="Comma-separated stage states to match"),
    ) -> dict:
        ids = store.list_session_ids(app.state.root)
        if days is not None:
            ids = [sid for sid in ids if _within_days(sid, days)]
        if status:
            wanted = {part.strip() for part in status.split(",") if part.strip()}
            ids = [sid for sid in ids if wanted & set(store.compute_status(app.state.root, sid).stages.values())]
        return {"sessions": ids, "latest": ids[-1] if ids else None}

    @app.get("/sessions/latest", response_model=SessionBundle)
    def latest() -> dict:
        session_id = store.latest_session_id(app.state.root)
        if session_id is None:
            raise HTTPException(status_code=404, detail="no sessions")
        return _bundle(app.state.root, session_id)

    @app.get("/sessions/{session_id}/status", response_model=SessionStatus)
    def session_status(session_id: str) -> dict:
        if not store.session_json_path(app.state.root, session_id).is_file():
            raise HTTPException(status_code=404, detail="session missing")
        with app.state.jobs_lock:
            running = sorted(app.state.jobs.get(session_id) or [])
        return store.compute_status(app.state.root, session_id, running=running).model_dump(mode="json")

    @app.get("/sessions/{session_id}/clips/{name}")
    def session_clip(session_id: str, name: str):
        if not serve_media_enabled():
            raise HTTPException(status_code=404, detail="not found")
        if not store.session_json_path(app.state.root, session_id).is_file():
            raise HTTPException(status_code=404, detail="session missing")
        safe = Path(name).name
        if not safe or safe != name or safe in {".", ".."}:
            raise HTTPException(status_code=404, detail="not found")
        path = store.clip_path(app.state.root, session_id, safe)
        try:
            path.resolve().relative_to(store.clips_dir(app.state.root, session_id).resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="not found") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(path)

    @app.get("/sessions/{session_id}", response_model=SessionBundle)
    def one_session(session_id: str) -> dict:
        return _bundle(app.state.root, session_id)

    @app.get("/sessions/{session_id}/transcript", response_model=Transcript)
    def transcript(session_id: str) -> dict:
        path = store.transcript_path(app.state.root, session_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="transcript missing")
        return store.load_transcript(app.state.root, session_id).model_dump(mode="json")

    @app.get("/sessions/{session_id}/insights", response_model=Insights)
    def insights(session_id: str) -> dict:
        path = store.insights_path(app.state.root, session_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="insights missing")
        return store.load_insights(app.state.root, session_id).model_dump(mode="json")

    @app.post("/sessions/{session_id}/run", response_model=RunAccepted, status_code=202)
    def run_session(
        session_id: str,
        stages: str = Query(default="transcribe,extract", description="Comma-separated stages"),
    ) -> dict:
        if not store.session_json_path(app.state.root, session_id).is_file():
            raise HTTPException(status_code=404, detail="session missing")
        requested = _parse_run_stages(stages)
        with app.state.jobs_lock:
            current = app.state.jobs.get(session_id) or set()
            if current:
                raise HTTPException(status_code=409, detail="run already in progress")
            app.state.jobs[session_id] = set(requested)
        status = store.compute_status(app.state.root, session_id, running=requested)
        thread = threading.Thread(
            target=_run_stages,
            args=(app, session_id, requested),
            name=f"tva-run-{session_id}",
            daemon=True,
        )
        thread.start()
        return {
            "accepted": True,
            "session_id": session_id,
            "stages": requested,
            "status": status.model_dump(mode="json"),
        }

    return app


def _parse_run_stages(raw: str) -> list[str]:
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not parts:
        raise HTTPException(status_code=400, detail="no stages")
    unknown = [item for item in parts if item not in ALLOWED_RUN_STAGES]
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown stages: {', '.join(unknown)}")
    # preserve order, unique
    seen: list[str] = []
    for item in parts:
        if item not in seen:
            seen.append(item)
    return seen


def _run_stages(app: FastAPI, session_id: str, stages: list[str]) -> None:
    error: str | None = None
    try:
        if "transcribe" in stages:
            transcribe_session(session_id, root=app.state.root)
        if "extract" in stages:
            extract_session(session_id, root=app.state.root)
    except Exception as exc:  # noqa: BLE001 — surface any stage failure on status
        error = str(exc)
    finally:
        with app.state.jobs_lock:
            app.state.jobs.pop(session_id, None)
        store.compute_status(app.state.root, session_id, error=error)


def _session_date(session_id: str) -> date | None:
    match = _SESSION_DATE.match(session_id)
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _within_days(session_id: str, days: int, *, today: date | None = None) -> bool:
    parsed = _session_date(session_id)
    if parsed is None:
        return False
    cutoff = (today or date.today()) - timedelta(days=days)
    return parsed >= cutoff


def _bundle(root: Path, session_id: str) -> dict:
    if not store.session_json_path(root, session_id).is_file():
        raise HTTPException(status_code=404, detail="session missing")
    payload: dict = {
        "session": store.load_session(root, session_id).model_dump(mode="json"),
        "status": store.compute_status(root, session_id).model_dump(mode="json"),
        "transcript": None,
        "insights": None,
    }
    if store.transcript_path(root, session_id).is_file():
        payload["transcript"] = store.load_transcript(root, session_id).model_dump(mode="json")
    if store.insights_path(root, session_id).is_file():
        payload["insights"] = store.load_insights(root, session_id).model_dump(mode="json")
    return payload
