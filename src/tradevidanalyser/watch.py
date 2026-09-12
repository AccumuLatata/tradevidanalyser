"""Copy-after-record: poll a local OBS folder, never record onto the NAS."""

from __future__ import annotations

import os
import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tradevidanalyser import config, media, store
from tradevidanalyser.ingest import ingest
from tradevidanalyser.naming import FilenameError, parse_obs_filename
from tradevidanalyser.pipeline import extract_session, transcribe_session

VIDEO_SUFFIXES = {".mp4", ".mkv"}
DEFAULT_STABLE_S = 15.0
DEFAULT_POLL_S = 2.0
ENV_STABLE_S = "TVA_WATCH_STABLE_S"
ENV_POLL_S = "TVA_WATCH_POLL_S"


class WatchError(RuntimeError):
    pass


def resolve_stable_s(value: float | None = None) -> float:
    if value is not None:
        return float(value)
    raw = (os.environ.get(ENV_STABLE_S) or "").strip()
    return float(raw) if raw else DEFAULT_STABLE_S


def resolve_poll_s(value: float | None = None) -> float:
    if value is not None:
        return float(value)
    raw = (os.environ.get(ENV_POLL_S) or "").strip()
    return float(raw) if raw else DEFAULT_POLL_S


def dest_path(root: Path, src: Path) -> Path:
    return config.recordings_dir(root) / src.name


def part_path(dest: Path) -> Path:
    return dest.with_name(dest.name + ".part")


def lock_path(root: Path, src: Path) -> Path:
    return config.recordings_dir(root) / f"{src.name}.lock"


def list_source_videos(source: Path) -> list[Path]:
    if not source.is_dir():
        raise WatchError(f"source is not a directory: {source}")
    found: list[Path] = []
    for path in sorted(source.iterdir()):
        if not path.is_file():
            continue
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if path.name.endswith(".part") or path.name.endswith(".lock"):
            continue
        found.append(path)
    return found


def mtime_stable(path: Path, *, stable_s: float, now: float | None = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age >= stable_s


def file_is_busy(path: Path) -> bool:
    """True if another process likely has the file open for write."""
    try:
        if sys.platform == "win32":
            fd = os.open(str(path), os.O_RDWR | getattr(os, "O_BINARY", 0))
            os.close(fd)
            return False
        import fcntl

        fd = os.open(str(path), os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
        except BlockingIOError:
            return True
        finally:
            os.close(fd)
        return False
    except OSError:
        return True


def already_ingested(root: Path, src: Path) -> bool:
    try:
        session_id, _ = parse_obs_filename(src)
    except FilenameError:
        return False
    session_path = store.session_json_path(root, session_id)
    if not session_path.is_file():
        return False
    digest = media.sha256_file(src)
    return store.load_session(root, session_id).recording.sha256 == digest


def _copy_with_lock(src: Path, *, root: Path, copy_fn: Callable[[Path, Path], None]) -> Path:
    dest = dest_path(root, src)
    part = part_path(dest)
    lock = lock_path(root, src)
    config.ensure_layout(root)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    try:
        copy_fn(src, part)
        part.replace(dest)
    finally:
        if part.exists():
            part.unlink()
        if lock.exists():
            lock.unlink()
    return dest


def process_source(
    source: Path,
    *,
    root: Path,
    run: bool = False,
    stable_s: float = DEFAULT_STABLE_S,
    now: float | None = None,
    copy_fn: Callable[[Path, Path], None] | None = None,
    is_busy: Callable[[Path], bool] | None = None,
) -> list[dict[str, Any]]:
    """Scan *source* once. Never deletes the source. Copies via .part + lock."""
    source = source.expanduser().resolve()
    root = root.expanduser().resolve()
    recordings = config.recordings_dir(root).resolve()
    if source == recordings or recordings in source.parents:
        raise WatchError("refusing to watch TVA_ROOT/recordings — record local, copy after")

    copier = copy_fn or shutil.copy2
    busy_fn = is_busy or file_is_busy
    events: list[dict[str, Any]] = []

    for src in list_source_videos(source):
        event: dict[str, Any] = {"file": src.name, "action": "skipped"}
        try:
            session_id, _ = parse_obs_filename(src)
        except FilenameError as exc:
            event["reason"] = str(exc)
            events.append(event)
            continue
        event["session_id"] = session_id

        lock = lock_path(root, src)
        if lock.is_file():
            event["reason"] = "locked"
            events.append(event)
            continue
        if not mtime_stable(src, stable_s=stable_s, now=now):
            event["reason"] = "unstable"
            events.append(event)
            continue
        if busy_fn(src):
            event["reason"] = "busy"
            events.append(event)
            continue
        if already_ingested(root, src):
            event["reason"] = "ingested"
            events.append(event)
            continue

        dest = dest_path(root, src)
        if dest.is_file() and media.sha256_file(dest) == media.sha256_file(src):
            record = ingest(dest, root=root)
        else:
            dest = _copy_with_lock(src, root=root, copy_fn=copier)
            record = ingest(dest, root=root)
            event["action"] = "copied"
            event["dest"] = dest.name
            if run:
                transcribe_session(record.id, root=root)
                extract_session(record.id, root=root)
                event["ran"] = True
            events.append(event)
            continue

        event["action"] = "skipped"
        event["reason"] = "dest-exists"
        if run:
            transcribe_session(record.id, root=root)
            extract_session(record.id, root=root)
            event["ran"] = True
        events.append(event)

    return events


def watch(
    source: Path,
    *,
    root: Path,
    once: bool = False,
    run: bool = False,
    stable_s: float | None = None,
    poll_s: float | None = None,
    copy_fn: Callable[[Path, Path], None] | None = None,
    is_busy: Callable[[Path], bool] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Poll *source* for finished OBS files and copy them into TVA_ROOT."""
    stable = resolve_stable_s(stable_s)
    poll = resolve_poll_s(poll_s)
    source = source.expanduser().resolve()
    root = root.expanduser().resolve()
    if not source.is_dir():
        raise WatchError(f"source is not a directory: {source}")

    all_events: list[dict[str, Any]] = []
    while True:
        all_events.extend(
            process_source(
                source,
                root=root,
                run=run,
                stable_s=stable,
                copy_fn=copy_fn,
                is_busy=is_busy,
            )
        )
        if once:
            break
        sleep_fn(poll)

    return {
        "source": str(source),
        "root": str(root),
        "once": once,
        "run": run,
        "stable_s": stable,
        "events": all_events,
    }
