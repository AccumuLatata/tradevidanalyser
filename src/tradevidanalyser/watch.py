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
# FAT/exFAT mtime is 2s; copy2 onto some NAS mounts also rounds.
_MTIME_MATCH_S = 2.0

DigestFn = Callable[[Path], str]
CopyFn = Callable[[Path, Path], None]
BusyFn = Callable[[Path], bool]
SleepFn = Callable[[float], None]
EventsFn = Callable[[list[dict[str, Any]]], None]


class WatchError(RuntimeError):
    pass


class _DigestCache:
    """Reuse sha256 while size+mtime are unchanged (continuous watch)."""

    def __init__(self) -> None:
        self._memo: dict[tuple[str, int, int], str] = {}

    def __call__(self, path: Path) -> str:
        st = path.stat()
        key = (str(path), st.st_size, st.st_mtime_ns)
        digest = self._memo.get(key)
        if digest is None:
            digest = media.sha256_file(path)
            self._memo[key] = digest
        return digest


def resolve_stable_s(value: float | None = None) -> float:
    if value is None:
        raw = (os.environ.get(ENV_STABLE_S) or "").strip()
        value = float(raw) if raw else DEFAULT_STABLE_S
    stable = float(value)
    if stable < 0 or stable != stable:
        raise WatchError(f"stable seconds must be >= 0, got {value!r}")
    return stable


def resolve_poll_s(value: float | None = None) -> float:
    if value is None:
        raw = (os.environ.get(ENV_POLL_S) or "").strip()
        value = float(raw) if raw else DEFAULT_POLL_S
    poll = float(value)
    if poll <= 0 or poll != poll:
        raise WatchError(f"poll interval must be > 0, got {value!r}")
    return poll


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
        if path.name.startswith("."):
            continue
        if path.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        found.append(path)
    return found


def mtime_stable(path: Path, *, stable_s: float, now: float | None = None) -> bool:
    try:
        age = (now if now is not None else time.time()) - path.stat().st_mtime
    except OSError:
        return False
    return age >= stable_s


def same_copy(src: Path, dest: Path) -> bool:
    """True when dest looks like shutil.copy2(src) (size + mtime)."""
    try:
        src_st = src.stat()
        dest_st = dest.stat()
    except OSError:
        return False
    if src_st.st_size != dest_st.st_size:
        return False
    return abs(src_st.st_mtime - dest_st.st_mtime) <= _MTIME_MATCH_S


def file_is_busy(path: Path) -> bool:
    """True if another process likely has the file open for write."""
    binary = getattr(os, "O_BINARY", 0)
    try:
        if sys.platform == "win32":
            try:
                fd = os.open(str(path), os.O_RDWR | binary)
            except OSError:
                fd = os.open(str(path), os.O_RDONLY | binary)
            os.close(fd)
            return False
        import fcntl

        fd = os.open(str(path), os.O_RDONLY)
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


def _pid_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _win_pid_is_running(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _win_pid_is_running(pid: int) -> bool:
    # os.kill(pid, 0) on Windows is TerminateProcess — do not use it.
    import ctypes

    kernel32 = ctypes.windll.kernel32
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    ERROR_ACCESS_DENIED = 5
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return kernel32.GetLastError() == ERROR_ACCESS_DENIED


def _lock_is_stale(lock: Path) -> bool:
    try:
        raw = lock.read_text(encoding="utf-8").strip().splitlines()
        pid = int(raw[0])
    except (OSError, ValueError, IndexError):
        return True
    return not _pid_is_running(pid)


def try_acquire_lock(lock: Path) -> bool:
    """Create ``lock`` with O_EXCL. Steal it only if the writer PID is dead."""
    lock.parent.mkdir(parents=True, exist_ok=True)
    for _attempt in range(2):
        try:
            fd = os.open(os.fspath(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _lock_is_stale(lock):
                try:
                    lock.unlink()
                except OSError:
                    return False
                continue
            return False
        try:
            os.write(fd, f"{os.getpid()}\n".encode("utf-8"))
        finally:
            os.close(fd)
        return True
    return False


def release_lock(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def already_ingested(
    root: Path,
    src: Path,
    *,
    digest_fn: DigestFn | None = None,
) -> bool:
    try:
        session_id, _ = parse_obs_filename(src)
    except FilenameError:
        return False
    session_path = store.session_json_path(root, session_id)
    if not session_path.is_file():
        return False
    try:
        session = store.load_session(root, session_id)
    except (OSError, ValueError):
        return False
    dest = dest_path(root, src)
    if dest.is_file() and same_copy(src, dest):
        return True
    hasher = digest_fn or media.sha256_file
    try:
        digest = hasher(src)
    except OSError:
        return False
    return session.recording.sha256 == digest


def _copy_via_part(src: Path, *, root: Path, copy_fn: CopyFn) -> Path:
    dest = dest_path(root, src)
    part = part_path(dest)
    config.ensure_layout(root)
    try:
        copy_fn(src, part)
        part.replace(dest)
    finally:
        if part.exists():
            part.unlink()
    return dest


def _copy_with_lock(src: Path, *, root: Path, copy_fn: CopyFn) -> Path:
    lock = lock_path(root, src)
    if not try_acquire_lock(lock):
        raise WatchError(f"locked: {src.name}")
    try:
        return _copy_via_part(src, root=root, copy_fn=copy_fn)
    finally:
        release_lock(lock)


def process_source(
    source: Path,
    *,
    root: Path,
    run: bool = False,
    stable_s: float = DEFAULT_STABLE_S,
    now: float | None = None,
    copy_fn: CopyFn | None = None,
    is_busy: BusyFn | None = None,
    digest_fn: DigestFn | None = None,
) -> list[dict[str, Any]]:
    """Scan *source* once. Never deletes the source. Copies via .part + lock."""
    source = source.expanduser().resolve()
    root = root.expanduser().resolve()
    recordings = config.recordings_dir(root).resolve()
    if source == recordings or recordings in source.parents:
        raise WatchError("refusing to watch TVA_ROOT/recordings — record local, copy after")

    copier = copy_fn or shutil.copy2
    busy_fn = is_busy or file_is_busy
    hasher = digest_fn or media.sha256_file
    events: list[dict[str, Any]] = []

    for src in list_source_videos(source):
        event: dict[str, Any] = {"file": src.name, "action": "skipped"}
        lock: Path | None = None
        acquired = False
        try:
            try:
                session_id, _ = parse_obs_filename(src)
            except FilenameError as exc:
                event["reason"] = str(exc)
                continue
            event["session_id"] = session_id

            if already_ingested(root, src, digest_fn=hasher):
                event["reason"] = "ingested"
                continue
            if not mtime_stable(src, stable_s=stable_s, now=now):
                event["reason"] = "unstable"
                continue
            if busy_fn(src):
                event["reason"] = "busy"
                continue

            lock = lock_path(root, src)
            if not try_acquire_lock(lock):
                event["reason"] = "locked"
                continue
            acquired = True

            if already_ingested(root, src, digest_fn=hasher):
                event["reason"] = "ingested"
                continue

            dest = dest_path(root, src)
            if dest.is_file() and (same_copy(src, dest) or hasher(dest) == hasher(src)):
                record = ingest(dest, root=root)
                event["reason"] = "dest-exists"
            else:
                dest = _copy_via_part(src, root=root, copy_fn=copier)
                record = ingest(dest, root=root)
                event["action"] = "copied"
                event["dest"] = dest.name

            if run:
                transcribe_session(record.id, root=root)
                extract_session(record.id, root=root)
                event["ran"] = True
        except WatchError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            event["action"] = "error"
            event["reason"] = str(exc)
        finally:
            if acquired and lock is not None:
                release_lock(lock)
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
    copy_fn: CopyFn | None = None,
    is_busy: BusyFn | None = None,
    sleep_fn: SleepFn = time.sleep,
    on_events: EventsFn | None = None,
) -> dict[str, Any]:
    """Poll *source* for finished OBS files and copy them into TVA_ROOT."""
    stable = resolve_stable_s(stable_s)
    poll = resolve_poll_s(poll_s)
    source = source.expanduser().resolve()
    root = root.expanduser().resolve()
    if not source.is_dir():
        raise WatchError(f"source is not a directory: {source}")

    hasher = _DigestCache()
    all_events: list[dict[str, Any]] = []
    while True:
        events = process_source(
            source,
            root=root,
            run=run,
            stable_s=stable,
            copy_fn=copy_fn,
            is_busy=is_busy,
            digest_fn=hasher,
        )
        if on_events is not None:
            on_events(events)
        if once:
            all_events.extend(events)
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
