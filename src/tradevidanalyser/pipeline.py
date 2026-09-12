"""Stage runners used by the CLI and by POST /sessions/{id}/run."""

from __future__ import annotations

from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.frames import FramesResult, extract_frames
from tradevidanalyser.ingest import ingest
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.extract import citation_problems, get_extract_provider
from tradevidanalyser.schema import Insights, SessionRecord, Transcript


def transcribe_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
) -> Transcript:
    record = store.load_session(root, session_id)
    audio = store.audio_path(root, session_id)
    if not audio.is_file():
        raise AsrError(f"no mic audio for {session_id}; run ingest first")
    provider = get_asr_provider(provider_name)
    transcript = provider.transcribe(audio, language=record.language)
    store.save_transcript(root, session_id, transcript)
    store.compute_status(
        root,
        session_id,
        cost_usd=getattr(provider, "last_cost_usd", None),
    )
    return transcript


def extract_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
) -> Insights:
    transcript = store.load_transcript(root, session_id)
    provider = get_extract_provider(provider_name)
    insights = provider.extract(transcript)
    _assert_citations(transcript, insights)
    store.save_insights(root, session_id, insights)
    store.compute_status(root, session_id)
    return insights


def _assert_citations(transcript: Transcript, insights: Insights) -> None:
    problems = citation_problems(transcript, insights)
    if problems:
        _field, _span, reason = problems[0]
        raise ValueError(reason)


def frames_session(
    session_id: str,
    *,
    root: Path,
    times: list[float] | None = None,
    contact_sheet: bool = False,
    layout_id: str | None = None,
) -> FramesResult:
    record = store.load_session(root, session_id)
    result = extract_frames(
        record,
        times,
        root=root,
        contact_sheet=contact_sheet,
        layout_id=layout_id,
    )
    store.compute_status(root, session_id)
    return result


def run_latest(
    root: Path, *, video: Path | None = None, desktop_track: bool = False
) -> SessionRecord:
    if video is not None:
        record = ingest(video, root=root, desktop_track=desktop_track)
    else:
        latest = store.latest_session_id(root)
        if latest is None:
            raise FileNotFoundError("no sessions under TVA_ROOT; pass a video to ingest")
        record = store.load_session(root, latest)
    transcribe_session(record.id, root=root)
    extract_session(record.id, root=root)
    return record
