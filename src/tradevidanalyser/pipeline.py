"""Stage runners used by the CLI and by POST /sessions/{id}/run."""

from __future__ import annotations

from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.align import Alignment, align_session as run_align
from tradevidanalyser.clips import ClipResult, extract_clips
from tradevidanalyser.fills import FillsResult, ingest_fills
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
from tradevidanalyser.evidence import EvidenceResult, evidence_session as run_evidence
from tradevidanalyser.context import ContextResult, context_session as run_context
from tradevidanalyser.ledger import (
    LedgerAddResult,
    RollupResult,
    add_session as run_ledger_add,
    rollup as run_rollup,
)
from tradevidanalyser.report import ReportResult, report_session as run_report
from tradevidanalyser.rules import RulesResult, rules_session as run_rules
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.extract import (
    citation_problems,
    evidence_citation_problems,
    get_extract_provider,
)
from tradevidanalyser.providers.vlm import (
    VlmContext,
    apply_visual_note_guard,
    get_vlm_provider,
    merge_visual_note_gaps,
    vlm_artifact,
)
from tradevidanalyser.schema import Evidence, Insights, SessionRecord, Transcript, VisualNote


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
    previous_notes = _visual_notes_to_preserve(root, session_id)
    provider = get_extract_provider(provider_name)
    insights = provider.extract(transcript)
    if previous_notes:
        insights = insights.model_copy(update={"visual_notes": previous_notes})
    _assert_citations(transcript, insights)
    if insights.visual_notes:
        ocr_rows = []
        ocr_path = store.ocr_path(root, session_id)
        if ocr_path.is_file():
            ocr_rows = read_ocr_parquet(ocr_path)
        kept, gaps = apply_visual_note_guard(
            insights.visual_notes,
            ocr_rows=ocr_rows,
            frame_stems={path.stem for path in store.list_frame_jpgs(root, session_id)},
            clip_names={path.name for path in store.list_clips(root, session_id)},
        )
        insights = insights.model_copy(
            update={
                "visual_notes": kept,
                "gaps": merge_visual_note_gaps(insights.gaps, gaps),
            }
        )
    store.save_insights(root, session_id, insights)
    store.compute_status(root, session_id)
    return insights


def _previous_vlm_cost(root: Path, session_id: str) -> float:
    path = store.visual_notes_path(root, session_id)
    if not path.is_file():
        return 0.0
    try:
        return float(store.read_json(path).get("cost_usd") or 0.0)
    except (TypeError, ValueError, OSError):
        return 0.0


def _visual_notes_to_preserve(root: Path, session_id: str) -> list[VisualNote]:
    insights_file = store.insights_path(root, session_id)
    if insights_file.is_file():
        try:
            notes = store.load_insights(root, session_id).visual_notes
            if notes:
                return list(notes)
        except (ValueError, OSError):
            pass
    artifact = store.visual_notes_path(root, session_id)
    if not artifact.is_file():
        return []
    try:
        raw = store.read_json(artifact).get("notes") or []
    except (ValueError, OSError):
        return []
    notes: list[VisualNote] = []
    if not isinstance(raw, list):
        return []
    for item in raw:
        try:
            notes.append(VisualNote.model_validate(item))
        except (TypeError, ValueError):
            continue
    return notes


def _assert_citations(
    transcript: Transcript,
    insights: Insights | None = None,
    evidence: Evidence | None = None,
) -> None:
    if insights is not None:
        problems = citation_problems(transcript, insights)
        if problems:
            _field, _span, reason = problems[0]
            raise ValueError(reason)
    if evidence is not None:
        problems = evidence_citation_problems(transcript, evidence)
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
    store.drop_debrief(root, session_id)
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
        root=root,
    )
    previous_vlm_cost = _previous_vlm_cost(root, session_id)
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
                "gaps": merge_visual_note_gaps(insights.gaps, gaps),
            }
        )
        store.save_insights(root, session_id, insights)
    # Fake reports 0; only bump status.cost_usd when the provider priced the call.
    # Re-runs replace the previous VLM slice instead of stacking it.
    cost: float | None = None
    priced = result.cost_usd or 0.0
    if priced or previous_vlm_cost:
        previous = None
        status_file = store.status_path(root, session_id)
        if status_file.is_file():
            try:
                previous = store.read_json(status_file).get("cost_usd")
            except (ValueError, OSError):
                previous = None
        cost = max(0.0, float(previous or 0.0) - previous_vlm_cost + priced)
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


def fills_session(
    session_id: str,
    *,
    root: Path,
    executions: Path,
    venue: str | None = None,
    include_manual: bool = False,
    prefer_import: bool | None = None,
    reconcile_dir: Path | None = None,
) -> FillsResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    record = store.load_session(root, session_id)
    return ingest_fills(
        record,
        executions,
        root=root,
        venue=venue,
        include_manual=include_manual,
        prefer_import=prefer_import,
        reconcile_dir=reconcile_dir,
    )


def evidence_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
    pre_s: float | None = None,
    post_s: float | None = None,
) -> EvidenceResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    result = run_evidence(
        session_id,
        root=root,
        provider_name=provider_name,
        pre_s=pre_s,
        post_s=post_s,
    )
    if result.status == "ok" and store.transcript_path(root, session_id).is_file():
        evidence_file = store.evidence_path(root, session_id)
        if evidence_file.is_file():
            try:
                _assert_citations(
                    store.load_transcript(root, session_id),
                    evidence=Evidence.model_validate(store.read_json(evidence_file)),
                )
            except ValueError:
                evidence_file.unlink(missing_ok=True)
                store.compute_status(root, session_id)
                raise
    return result


def rules_session(
    session_id: str,
    *,
    root: Path,
    config_path: Path | None = None,
) -> RulesResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return run_rules(session_id, root=root, config_path=config_path)


def context_session(
    session_id: str,
    *,
    root: Path,
    lab_dir: Path | None = None,
    notion_provider: str | None = None,
) -> ContextResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return run_context(
        session_id,
        root=root,
        lab_dir=lab_dir,
        notion_provider=notion_provider,
    )


def report_session(
    session_id: str,
    *,
    root: Path,
    provider_name: str | None = None,
) -> ReportResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return run_report(session_id, root=root, provider_name=provider_name)


def ledger_add(session_id: str, *, root: Path) -> LedgerAddResult:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return run_ledger_add(session_id, root=root)


def rollup_ledger(
    root: Path,
    *,
    week: str | None = None,
    month: str | None = None,
) -> RollupResult:
    return run_rollup(root, week=week, month=month)


def align_session(
    session_id: str,
    *,
    root: Path,
    manual_offset: float | None = None,
) -> Alignment:
    if not store.is_safe_path_name(session_id):
        raise ValueError(f"unsafe session id {session_id!r}")
    return run_align(session_id, root=root, manual_offset=manual_offset)


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
