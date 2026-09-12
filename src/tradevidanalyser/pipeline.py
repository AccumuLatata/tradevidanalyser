"""Stage runners used by the CLI and by POST /sessions/{id}/run."""

from __future__ import annotations

from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.clips import ClipResult, extract_clips
from tradevidanalyser.frames import FramesResult, extract_frames
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ocr import (
    OcrProvider,
    get_ocr_provider,
    ocr_frames,
    ocr_result_dict,
    read_ocr_parquet,
    write_ocr_parquet,
)
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.extract import citation_problems, get_extract_provider
from tradevidanalyser.providers.vlm import (
    VlmContext,
    apply_visual_note_guard,
    get_vlm_provider,
    visual_note_problems,
    vlm_artifact,
)
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
    previous_notes = []
    insights_file = store.insights_path(root, session_id)
    if insights_file.is_file():
        try:
            previous_notes = store.load_insights(root, session_id).visual_notes
        except (ValueError, OSError):
            previous_notes = []
    provider = get_extract_provider(provider_name)
    insights = provider.extract(transcript)
    if previous_notes:
        insights = insights.model_copy(update={"visual_notes": previous_notes})
    _assert_citations(transcript, insights, root=root, session_id=session_id)
    store.save_insights(root, session_id, insights)
    store.compute_status(root, session_id)
    return insights


def _assert_citations(
    transcript: Transcript,
    insights: Insights,
    *,
    root: Path | None = None,
    session_id: str | None = None,
) -> None:
    problems = citation_problems(transcript, insights)
    if problems:
        _field, _span, reason = problems[0]
        raise ValueError(reason)
    if not insights.visual_notes:
        return
    ocr_rows = []
    stems: set[str] = set()
    clip_names: set[str] = set()
    if root is not None and session_id is not None:
        stems = {path.stem for path in store.list_frame_jpgs(root, session_id)}
        clip_names = {path.name for path in store.list_clips(root, session_id)}
        ocr_path = store.ocr_path(root, session_id)
        if ocr_path.is_file():
            ocr_rows = read_ocr_parquet(ocr_path)
    extra = visual_note_problems(
        insights.visual_notes,
        ocr_rows=ocr_rows,
        frame_stems=stems,
        clip_names=clip_names,
    )
    if extra:
        raise ValueError(extra[0][1])


def frames_session(
    session_id: str,
    *,
    root: Path,
    times: list[float] | None = None,
    contact_sheet: bool = False,
    layout_id: str | None = None,
) -> FramesResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
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


def ocr_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
    layout_id: str | None = None,
) -> dict:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    provider: OcrProvider = get_ocr_provider(provider_name)
    rows = ocr_frames(record, root=root, provider=provider, layout_id=layout_id)
    path = store.ocr_path(root, session_id)
    write_ocr_parquet(path, rows)
    store.compute_status(root, session_id)
    payload = ocr_result_dict(session_id, path, rows, provider)
    try:
        payload["path"] = str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        pass
    return payload


def clips_session(
    session_id: str,
    *,
    root: Path,
    redact: bool = False,
    layout_id: str | None = None,
) -> ClipResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    result = extract_clips(record, root=root, redact=redact, layout_id=layout_id)
    store.compute_status(root, session_id)
    return result


def vlm_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
) -> dict:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    provider = get_vlm_provider(provider_name)
    if provider is None:
        return {
            "status": "skipped",
            "reason": "TVA_VLM_PROVIDER unset",
            "session_id": session_id,
        }
    record = store.load_session(root, session_id)
    frames = store.list_frame_jpgs(root, session_id)
    clips = store.list_clips(root, session_id)
    if not frames and not clips:
        return {
            "status": "skipped",
            "reason": "no redacted frames or clips",
            "session_id": session_id,
        }
    ocr_rows = []
    ocr_path = store.ocr_path(root, session_id)
    if ocr_path.is_file():
        ocr_rows = read_ocr_parquet(ocr_path)
    ctx = VlmContext(
        session_id=session_id,
        frames=frames,
        clips=clips,
        ocr_rows=ocr_rows,
        recording_path=record.recording.path,
    )
    result = provider.annotate(ctx)
    stems = {path.stem for path in frames}
    clip_names = {path.name for path in clips}
    kept, gaps = apply_visual_note_guard(
        result.notes,
        ocr_rows=ocr_rows,
        frame_stems=stems,
        clip_names=clip_names,
    )
    result.notes = kept
    artifact = vlm_artifact(session_id, result, provider=provider.name, gaps=gaps)
    store.write_json(store.visual_notes_path(root, session_id), artifact)
    insights_file = store.insights_path(root, session_id)
    if insights_file.is_file():
        try:
            insights = store.load_insights(root, session_id)
        except (ValueError, OSError) as exc:
            raise ValueError(f"insights.json is unreadable: {exc}") from exc
        insights = insights.model_copy(
            update={
                "visual_notes": kept,
                "gaps": sorted(set(insights.gaps) | set(gaps)),
            }
        )
        store.save_insights(root, session_id, insights)
    # Fake reports 0; only bump status.cost_usd when the provider priced the call.
    cost: float | None = None
    priced = result.cost_usd or 0.0
    if priced:
        previous = None
        status_file = store.status_path(root, session_id)
        if status_file.is_file():
            try:
                previous = store.read_json(status_file).get("cost_usd")
            except (ValueError, OSError):
                previous = None
        cost = float(previous or 0.0) + priced
    store.compute_status(root, session_id, cost_usd=cost)
    payload = {
        "status": "ok",
        "session_id": session_id,
        "provider": provider.name,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "notes": [note.model_dump(mode="json") for note in kept],
        "gaps": gaps,
        "cost_usd": result.cost_usd,
        "frames_used": result.frames_used,
        "clip": result.clip,
        "path": "visual_notes.json",
    }
    return payload


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
