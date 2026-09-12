"""Stage runners used by the CLI and by POST /sessions/{id}/run."""

from __future__ import annotations

from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.ingest import ingest
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.extract import get_extract_provider
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
    store.compute_status(root, session_id)
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
    known = {seg.id: seg.text for seg in transcript.segments}
    fields = (
        insights.bias_statements,
        insights.playbooks_mentioned,
        insights.stated_levels,
        insights.stated_stops_targets,
        insights.checkins,
        insights.tilt_markers,
        insights.brief_refs,
        insights.observations,
    )
    for group in fields:
        for span in group:
            if span.seg not in known:
                raise ValueError(f"citation {span.seg} is not in the transcript")
            if span.text and span.text not in known[span.seg]:
                raise ValueError(f"quote not found in {span.seg}")


def run_latest(root: Path, *, video: Path | None = None) -> SessionRecord:
    if video is not None:
        record = ingest(video, root=root)
    else:
        latest = store.latest_session_id(root)
        if latest is None:
            raise FileNotFoundError("no sessions under TVA_ROOT; pass a video to ingest")
        record = store.load_session(root, latest)
    transcribe_session(record.id, root=root)
    extract_session(record.id, root=root)
    return record
