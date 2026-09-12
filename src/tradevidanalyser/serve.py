"""Read-only HTTP API over TVA_ROOT. Bind to localhost or a Tailscale interface."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from tradevidanalyser import __version__, store
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.pipeline import extract_session, transcribe_session


def create_app(root: Path) -> FastAPI:
    app = FastAPI(title="TradeVidAnalyser", version=__version__)
    app.state.root = root.resolve()

    @app.get("/health")
    def health() -> JSONResponse:
        report = run_doctor(app.state.root)
        return JSONResponse(report.model_dump(mode="json"))

    @app.get("/sessions")
    def sessions() -> dict:
        ids = store.list_session_ids(app.state.root)
        return {"sessions": ids, "latest": ids[-1] if ids else None}

    @app.get("/sessions/latest")
    def latest() -> dict:
        session_id = store.latest_session_id(app.state.root)
        if session_id is None:
            raise HTTPException(status_code=404, detail="no sessions")
        return _bundle(app.state.root, session_id)

    @app.get("/sessions/{session_id}")
    def one_session(session_id: str) -> dict:
        return _bundle(app.state.root, session_id)

    @app.get("/sessions/{session_id}/transcript")
    def transcript(session_id: str) -> dict:
        path = store.transcript_path(app.state.root, session_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="transcript missing")
        return store.load_transcript(app.state.root, session_id).model_dump(mode="json")

    @app.get("/sessions/{session_id}/insights")
    def insights(session_id: str) -> dict:
        path = store.insights_path(app.state.root, session_id)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="insights missing")
        return store.load_insights(app.state.root, session_id).model_dump(mode="json")

    @app.post("/sessions/{session_id}/run")
    def run_session(session_id: str) -> dict:
        if not store.session_json_path(app.state.root, session_id).is_file():
            raise HTTPException(status_code=404, detail="session missing")
        transcribe_session(session_id, root=app.state.root)
        extract_session(session_id, root=app.state.root)
        return _bundle(app.state.root, session_id)

    return app


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
