from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from tradevidanalyser import config, media, store
from tradevidanalyser.cli import main
from tradevidanalyser.ingest import ingest
from tradevidanalyser.watch import (
    WatchError,
    dest_path,
    lock_path,
    process_source,
    watch,
)


def _obs_name(tmp: Path, name: str = "2026-09-11 14-30-00.mp4") -> Path:
    return tmp / name


def test_growing_file_is_not_picked_up(tva_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    growing = _obs_name(source)
    growing.write_bytes(b"partial-obs-bytes")
    report = watch(source, root=tva_root, once=True, stable_s=60)
    dest = dest_path(tva_root, growing)
    assert not dest.exists()
    assert growing.exists()
    assert growing.read_bytes() == b"partial-obs-bytes"
    assert report["events"][0]["reason"] == "unstable"
    assert not lock_path(tva_root, growing).exists()


def test_stable_file_is_copied_once_and_lock_removed(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    copies: list[Path] = []

    def copy_fn(origin: Path, dest: Path) -> None:
        lock = lock_path(tva_root, origin)
        assert lock.is_file()
        copies.append(origin)
        shutil.copy2(origin, dest)

    first = watch(source, root=tva_root, once=True, stable_s=0, copy_fn=copy_fn)
    dest = dest_path(tva_root, src)
    assert dest.is_file()
    assert src.exists()
    assert media.sha256_file(dest) == media.sha256_file(src)
    assert first["events"][0]["action"] == "copied"
    assert first["events"][0]["session_id"] == "2026-09-11_143000"
    assert not lock_path(tva_root, src).exists()
    assert not dest_path(tva_root, src).with_name(src.name + ".part").exists()
    assert store.session_json_path(tva_root, "2026-09-11_143000").is_file()

    second = watch(source, root=tva_root, once=True, stable_s=0, copy_fn=copy_fn)
    assert len(copies) == 1
    assert second["events"][0]["action"] == "skipped"
    assert second["events"][0]["reason"] == "ingested"
    assert src.exists()


def test_rerun_is_noop_when_already_ingested(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    ingest(src, root=tva_root)
    copies: list[Path] = []
    watch(
        source,
        root=tva_root,
        once=True,
        stable_s=0,
        copy_fn=lambda a, b: copies.append(a),
    )
    assert copies == []
    assert src.exists()


def test_busy_file_is_not_picked_up(tva_root: Path, tmp_path: Path, write_video) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    report = watch(source, root=tva_root, once=True, stable_s=0, is_busy=lambda _p: True)
    assert not dest_path(tva_root, src).exists()
    assert report["events"][0]["reason"] == "busy"
    assert src.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl flock is Unix-only")
def test_flocked_file_is_busy(tva_root: Path, tmp_path: Path, write_video) -> None:
    import fcntl

    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    fd = os.open(src, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        report = watch(source, root=tva_root, once=True, stable_s=0)
        assert report["events"][0]["reason"] == "busy"
        assert not dest_path(tva_root, src).exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert src.exists()


def test_refuses_to_watch_recordings_dir(tva_root: Path) -> None:
    with pytest.raises(WatchError, match="record local"):
        watch(config.recordings_dir(tva_root), root=tva_root, once=True, stable_s=0)


def test_cli_watch_once(tva_root: Path, tmp_path: Path, write_video, capsys) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    write_video(_obs_name(source))
    code = main(
        [
            "--root",
            str(tva_root),
            "watch",
            "--source",
            str(source),
            "--once",
            "--stable-seconds",
            "0",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["once"] is True
    assert payload["events"][0]["action"] == "copied"
    assert payload["events"][0]["session_id"] == "2026-09-11_143000"


def test_watch_run_uses_fake_pipeline(tva_root: Path, tmp_path: Path, write_video) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    write_video(_obs_name(source))
    watch(source, root=tva_root, once=True, run=True, stable_s=0)
    sid = "2026-09-11_143000"
    assert store.transcript_path(tva_root, sid).is_file()
    assert store.insights_path(tva_root, sid).is_file()


def test_process_source_skips_non_obs_name(tva_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    (source / "session.mp4").write_bytes(b"x")
    events = process_source(source, root=tva_root, stable_s=0)
    assert events[0]["action"] == "skipped"
    assert "OBS" in events[0]["reason"]


def test_hidden_and_appledouble_files_are_ignored(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    write_video(source / "._2026-09-11 14-30-00.mp4")
    (source / ".hidden.mp4").write_bytes(b"nope")
    events = process_source(source, root=tva_root, stable_s=0)
    assert events == []


def test_live_lock_skips_copy(tva_root: Path, tmp_path: Path, write_video) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    lock = lock_path(tva_root, src)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    copies: list[Path] = []
    report = watch(
        source,
        root=tva_root,
        once=True,
        stable_s=0,
        copy_fn=lambda a, b: copies.append(a),
    )
    assert copies == []
    assert report["events"][0]["reason"] == "locked"
    assert src.exists()
    assert lock.is_file()


def test_stale_lock_is_recovered(tva_root: Path, tmp_path: Path, write_video) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    lock = lock_path(tva_root, src)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("2147483647\n", encoding="utf-8")
    report = watch(source, root=tva_root, once=True, stable_s=0)
    assert report["events"][0]["action"] == "copied"
    assert dest_path(tva_root, src).is_file()
    assert not lock.exists()
    assert src.exists()


def test_dest_exists_same_copy_ingests_without_recopy(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    dest = dest_path(tva_root, src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    copies: list[Path] = []
    report = watch(
        source,
        root=tva_root,
        once=True,
        stable_s=0,
        copy_fn=lambda a, b: copies.append(a),
    )
    assert copies == []
    assert report["events"][0]["reason"] == "dest-exists"
    assert store.session_json_path(tva_root, "2026-09-11_143000").is_file()
    assert src.exists()


def test_already_ingested_does_not_hash_when_dest_matches(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    write_video(_obs_name(source))
    watch(source, root=tva_root, once=True, stable_s=0)
    hashes = {"n": 0}
    real = media.sha256_file

    def spy(path: Path, *, chunk: int = 1024 * 1024) -> str:
        hashes["n"] += 1
        return real(path, chunk=chunk)

    monkeypatch.setattr("tradevidanalyser.watch.media.sha256_file", spy)
    report = watch(source, root=tva_root, once=True, stable_s=0)
    assert report["events"][0]["reason"] == "ingested"
    assert hashes["n"] == 0


def test_one_file_error_does_not_block_sibling(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    good = write_video(source / "2026-09-11 14-30-00.mp4")
    write_video(source / "2026-09-11 14-31-00.mp4")

    def copy_fn(origin: Path, dest: Path) -> None:
        if "14-31-00" in origin.name:
            raise OSError("disk full")
        shutil.copy2(origin, dest)

    events = process_source(source, root=tva_root, stable_s=0, copy_fn=copy_fn)
    by_file = {event["file"]: event for event in events}
    assert by_file[good.name]["action"] == "copied"
    assert by_file["2026-09-11 14-31-00.mp4"]["action"] == "error"
    assert dest_path(tva_root, good).is_file()
    assert store.session_json_path(tva_root, "2026-09-11_143000").is_file()
    assert not dest_path(tva_root, source / "2026-09-11 14-31-00.mp4").exists()


def test_refuses_to_watch_recordings_subdir(tva_root: Path) -> None:
    nested = config.recordings_dir(tva_root) / "nested"
    nested.mkdir()
    with pytest.raises(WatchError, match="record local"):
        watch(nested, root=tva_root, once=True, stable_s=0)


def test_continuous_watch_does_not_accumulate_events(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    write_video(_obs_name(source))
    seen: list[list[dict]] = []

    def on_events(events: list[dict]) -> None:
        seen.append(list(events))

    def sleep_fn(_seconds: float) -> None:
        if len(seen) >= 2:
            raise RuntimeError("stop-watch")

    with pytest.raises(RuntimeError, match="stop-watch"):
        watch(
            source,
            root=tva_root,
            once=False,
            stable_s=0,
            poll_s=0.01,
            on_events=on_events,
            sleep_fn=sleep_fn,
        )
    assert seen[0][0]["action"] == "copied"
    assert seen[1][0]["reason"] == "ingested"


def test_poll_interval_must_be_positive(tva_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    with pytest.raises(WatchError, match="poll interval"):
        watch(source, root=tva_root, once=True, poll_s=0)


@pytest.mark.skipif(sys.platform == "win32", reason="chmod(0444) is Unix-oriented")
def test_readonly_source_is_still_copied(tva_root: Path, tmp_path: Path, write_video) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    src.chmod(0o444)
    try:
        report = watch(source, root=tva_root, once=True, stable_s=0)
        assert report["events"][0]["action"] == "copied"
        assert dest_path(tva_root, src).is_file()
    finally:
        src.chmod(0o644)
        assert src.exists()
