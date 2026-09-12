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
