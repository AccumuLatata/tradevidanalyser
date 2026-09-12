"""Register an OBS recording and extract the first audio track."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tradevidanalyser import __version__, config, media, store
from tradevidanalyser.naming import FilenameError, obs_name_prefix, parse_obs_filename
from tradevidanalyser.schema import Chapter, RecordingInfo, RecordingPart, SessionRecord

SPLIT_GAP_S = 5.0


@dataclass
class _Part:
    path: Path
    session_id: str
    start: datetime
    duration_s: float
    probe: dict
    digest: str


def ingest(video: Path, *, root: Path, desktop_track: bool = False) -> SessionRecord:
    video = video.expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(video)

    config.ensure_layout(root)
    chain = discover_split_parts(video)
    first = chain[0]
    session_id = first.session_id
    start = first.start

    dests = _copy_parts(chain, root=root)
    parts = _recording_parts(dests, root=root)
    digest = dests[0][1]
    existing_path = store.session_json_path(root, session_id)
    if existing_path.is_file():
        existing = store.load_session(root, session_id)
        existing_shas = [p.sha256 for p in existing.recording.parts]
        planned_shas = [p.sha256 for p in parts]
        if existing.recording.sha256 == digest and existing_shas == planned_shas:
            store.compute_status(root, session_id)
            return existing

    dest_files = [path for path, _digest in dests]
    chapters = _shifted_chapters(chain)
    tracks = media.audio_track_labels(first.probe)
    if not tracks and _has_audio(first.probe):
        tracks = ["mic"]
    if desktop_track and "desktop" not in tracks:
        tracks = [*tracks, "desktop"]

    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=str(dest_files[0].relative_to(root)),
            sha256=digest,
            start_wallclock_vienna=start.isoformat(),
            duration_s=sum(p.duration_s for p in parts) if parts else first.duration_s,
            tracks=tracks or [],
            chapters=chapters,
            filename=dest_files[0].name,
            parts=parts,
        ),
        language="de",
        app_version=__version__,
    )
    store.save_session(root, record)

    try:
        _write_session_audio(dest_files, root=root, session_id=session_id, desktop_track=desktop_track)
    except media.MediaError:
        pass

    store.compute_status(root, session_id)
    return record


def discover_split_parts(video: Path) -> list[_Part]:
    """OBS auto-split siblings: same date prefix, consecutive starts within 5 s of duration."""
    video = video.expanduser().resolve()
    try:
        parse_obs_filename(video)
    except FilenameError:
        raise

    candidates: list[_Part] = []
    for path in sorted(video.parent.iterdir()):
        if not path.is_file() or path.suffix.lower() != video.suffix.lower():
            continue
        if not _same_prefix(video, path):
            continue
        try:
            session_id, start = parse_obs_filename(path)
        except FilenameError:
            continue
        probe = media.ffprobe(path)
        candidates.append(
            _Part(
                path=path.resolve(),
                session_id=session_id,
                start=start,
                duration_s=media.duration_s(probe),
                probe=probe,
                digest=media.sha256_file(path),
            )
        )
    if not candidates:
        probe = media.ffprobe(video)
        session_id, start = parse_obs_filename(video)
        return [
            _Part(
                path=video,
                session_id=session_id,
                start=start,
                duration_s=media.duration_s(probe),
                probe=probe,
                digest=media.sha256_file(video),
            )
        ]

    candidates.sort(key=lambda item: item.start)
    chains: list[list[_Part]] = [[candidates[0]]]
    for prev, nxt in zip(candidates, candidates[1:], strict=False):
        gap = (nxt.start - prev.start).total_seconds() - prev.duration_s
        if abs(gap) <= SPLIT_GAP_S:
            chains[-1].append(nxt)
        else:
            chains.append([nxt])

    for chain in chains:
        if any(item.path == video for item in chain):
            return chain
    return [item for item in candidates if item.path == video] or [candidates[0]]


def _same_prefix(anchor: Path, other: Path) -> bool:
    left = obs_name_prefix(anchor)
    right = obs_name_prefix(other)
    return left is not None and left == right


def _copy_parts(chain: list[_Part], *, root: Path) -> list[tuple[Path, str]]:
    recordings = config.recordings_dir(root)
    dests: list[tuple[Path, str]] = []
    for item in chain:
        dest = recordings / item.path.name
        if dest.resolve() != item.path:
            if not dest.is_file() or media.sha256_file(dest) != item.digest:
                shutil.copy2(item.path, dest)
        dests.append((dest, media.sha256_file(dest)))
    return dests


def _recording_parts(dests: list[tuple[Path, str]], *, root: Path) -> list[RecordingPart]:
    if len(dests) < 2:
        return []
    parts: list[RecordingPart] = []
    offset = 0.0
    for path, digest in dests:
        probe = media.ffprobe(path)
        duration = media.duration_s(probe)
        parts.append(
            RecordingPart(
                path=str(path.relative_to(root)),
                sha256=digest,
                duration_s=duration,
                offset_s=offset,
                filename=path.name,
            )
        )
        offset += duration
    return parts


def _shifted_chapters(chain: list[_Part]) -> list[Chapter]:
    chapters: list[Chapter] = []
    offset = 0.0
    for item in chain:
        for chapter in media.chapters_from_probe(item.probe):
            chapters.append(Chapter(t=chapter.t + offset, name=chapter.name))
        offset += item.duration_s
    return chapters


def _write_session_audio(
    dest_files: list[Path],
    *,
    root: Path,
    session_id: str,
    desktop_track: bool,
) -> None:
    mic_pieces: list[Path] = []
    desk_pieces: list[Path] = []
    scratch = config.session_dir(root, session_id) / "audio" / ".parts"
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        for index, src in enumerate(dest_files):
            mic = scratch / f"mic_{index:03d}.opus"
            media.extract_track(src, 0, mic)
            mic_pieces.append(mic)
        media.concat_audio(mic_pieces, store.audio_path(root, session_id))
        if desktop_track:
            try:
                for index, src in enumerate(dest_files):
                    desk = scratch / f"desk_{index:03d}.opus"
                    media.extract_track(src, 1, desk)
                    desk_pieces.append(desk)
                media.concat_audio(desk_pieces, store.desktop_audio_path(root, session_id))
            except media.MediaError:
                pass
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _has_audio(probe: dict) -> bool:
    return any(s.get("codec_type") == "audio" for s in probe.get("streams") or [])
