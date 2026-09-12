"""Register an OBS recording and extract the first audio track."""

from __future__ import annotations

import shutil
from pathlib import Path

from tradevidanalyser import __version__, config, media, store
from tradevidanalyser.naming import parse_obs_filename
from tradevidanalyser.schema import RecordingInfo, SessionRecord


def ingest(video: Path, *, root: Path) -> SessionRecord:
    video = video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)

    session_id, start = parse_obs_filename(video)
    config.ensure_layout(root)

    digest = media.sha256_file(video)
    existing_path = store.session_json_path(root, session_id)
    if existing_path.is_file():
        existing = store.load_session(root, session_id)
        if existing.recording.sha256 == digest:
            store.compute_status(root, session_id)
            return existing

    recordings = config.recordings_dir(root)
    dest = recordings / video.name
    if dest.resolve() != video:
        if not dest.is_file() or media.sha256_file(dest) != digest:
            shutil.copy2(video, dest)

    probe = media.ffprobe(dest)
    tracks = media.audio_track_labels(probe)
    if not tracks and _has_audio(probe):
        tracks = ["mic"]

    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=str(dest.relative_to(root)),
            sha256=digest,
            start_wallclock_vienna=start.isoformat(),
            duration_s=media.duration_s(probe),
            tracks=tracks or [],
            chapters=media.chapters_from_probe(probe),
            filename=video.name,
        ),
        language="de",
        app_version=__version__,
    )
    store.save_session(root, record)

    if tracks:
        try:
            media.extract_mic_opus(dest, store.audio_path(root, session_id))
        except media.MediaError:
            # Video without a usable first audio stream is still a registered session.
            pass

    store.compute_status(root, session_id)
    return record


def _has_audio(probe: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])
