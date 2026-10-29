"""PR-39: copy <stem>.jsonl under TVA_SIDECAR. Tests use tmp dirs only."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tradevidanalyser import config
from tradevidanalyser.cli import main
from tradevidanalyser.flags import ENV_SIDECAR
from tradevidanalyser.ingest import ingest
from tradevidanalyser.sidecar_copy import (
    SIDECAR_INVALID_JSON,
    SIDECAR_MISSING,
    SIDECAR_OK,
    classify_jsonl,
    copy_sidecar,
    sidecar_dest_path,
    sidecar_source_path,
)
from tradevidanalyser.watch import dest_path, process_source, watch

SRC = Path("src/tradevidanalyser")


def _obs_name(tmp: Path, name: str = "2026-09-11 14-30-00.mp4") -> Path:
    return tmp / name


def _write_jsonl(path: Path, rows: list[dict] | str) -> Path:
    if isinstance(rows, str):
        path.write_text(rows, encoding="utf-8")
    else:
        path.write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    return path


def test_sidecar_dest_is_under_root(tva_root: Path) -> None:
    video = Path("2026-09-11 14-30-00.mp4")
    dest = sidecar_dest_path(tva_root, video)
    assert dest == config.recordings_dir(tva_root) / "2026-09-11 14-30-00.jsonl"
    assert dest.is_relative_to(tva_root)


def test_sidecar_source_is_stem_jsonl_not_events() -> None:
    video = Path("obs") / "2026-09-11 14-30-00.mp4"
    assert sidecar_source_path(video) == video.with_suffix(".jsonl")
    assert sidecar_source_path(video).name == "2026-09-11 14-30-00.jsonl"
    assert not sidecar_source_path(video).name.endswith(".events.jsonl")


def test_classify_jsonl_ok_missing_invalid(tmp_path: Path) -> None:
    missing = tmp_path / "nope.jsonl"
    assert classify_jsonl(missing) == SIDECAR_MISSING
    ok = _write_jsonl(tmp_path / "ok.jsonl", [{"event": "record_start"}])
    assert classify_jsonl(ok) == SIDECAR_OK
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert classify_jsonl(empty) == SIDECAR_OK
    bad = _write_jsonl(tmp_path / "bad.jsonl", "{not json\n")
    assert classify_jsonl(bad) == SIDECAR_INVALID_JSON
    binary = tmp_path / "bin.jsonl"
    binary.write_bytes(b"\xff\xfe not utf-8")
    assert classify_jsonl(binary) == SIDECAR_INVALID_JSON


def test_copy_sidecar_binary_is_invalid_json_and_still_copied(tmp_path: Path) -> None:
    src_video = tmp_path / "src" / "clip.mp4"
    dest_video = tmp_path / "dest" / "clip.mp4"
    src_video.parent.mkdir()
    dest_video.parent.mkdir()
    src_video.write_bytes(b"x")
    dest_video.write_bytes(b"x")
    payload = b"\xff\xfe not utf-8"
    src_video.with_suffix(".jsonl").write_bytes(payload)
    assert copy_sidecar(src_video, dest_video) == SIDECAR_INVALID_JSON
    dest = dest_video.with_suffix(".jsonl")
    assert dest.is_file()
    assert dest.read_bytes() == payload


def test_failed_copy_leaves_old_dest_intact(tmp_path: Path) -> None:
    src_video = tmp_path / "src" / "clip.mp4"
    dest_video = tmp_path / "dest" / "clip.mp4"
    src_video.parent.mkdir()
    dest_video.parent.mkdir()
    src_video.write_bytes(b"x")
    dest_video.write_bytes(b"x")
    _write_jsonl(src_video.with_suffix(".jsonl"), [{"event": "new"}])
    dest = dest_video.with_suffix(".jsonl")
    dest.write_text('{"event": "old"}\n', encoding="utf-8")
    old = dest.read_text(encoding="utf-8")

    def boom(_src: Path, part: Path) -> None:
        assert part.name.endswith(".part")
        assert part != dest
        part.write_bytes(b"partial-sidecar")
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        copy_sidecar(src_video, dest_video, only_if_absent=False, copy_fn=boom)
    assert dest.read_text(encoding="utf-8") == old
    assert not dest.with_name(dest.name + ".part").exists()


def test_copy_sidecar_skips_events_jsonl(tmp_path: Path) -> None:
    src_video = tmp_path / "src" / "clip.mp4"
    dest_video = tmp_path / "dest" / "clip.mp4"
    src_video.parent.mkdir()
    dest_video.parent.mkdir()
    src_video.write_bytes(b"x")
    dest_video.write_bytes(b"x")
    events = src_video.with_name("clip.events.jsonl")
    events.write_text('{"event": "x"}\n', encoding="utf-8")
    assert copy_sidecar(src_video, dest_video) == SIDECAR_MISSING
    assert not dest_video.with_suffix(".jsonl").exists()
    assert not dest_video.with_name("clip.events.jsonl").exists()


def test_flag_off_watch_does_not_copy_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_SIDECAR, raising=False)
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "record_start"}])
    _write_jsonl(src.with_name(src.stem + ".events.jsonl"), [{"event": "ignored"}])
    report = watch(source, root=tva_root, once=True, stable_s=0)
    dest = dest_path(tva_root, src)
    assert dest.is_file()
    assert not dest.with_suffix(".jsonl").exists()
    assert not dest.with_name(src.stem + ".events.jsonl").exists()
    assert "sidecar" not in report
    assert "sidecar" not in report["events"][0]


def test_flag_on_missing_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    report = watch(source, root=tva_root, once=True, stable_s=0)
    dest = dest_path(tva_root, src)
    assert dest.is_file()
    assert not sidecar_dest_path(tva_root, src).exists()
    assert report["events"][0]["sidecar"] == SIDECAR_MISSING
    assert report["sidecar"] == {"ok": 0, "missing": 1, "invalid_json": 0}


def test_flag_on_copies_stem_jsonl(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    payload = [{"event": "record_start", "file": src.name}]
    _write_jsonl(src.with_suffix(".jsonl"), payload)
    _write_jsonl(src.with_name(src.stem + ".events.jsonl"), [{"event": "nope"}])
    report = watch(source, root=tva_root, once=True, stable_s=0)
    dest = dest_path(tva_root, src)
    sidecar = sidecar_dest_path(tva_root, src)
    assert sidecar.is_file()
    assert sidecar.parent == config.recordings_dir(tva_root)
    assert sidecar.read_text(encoding="utf-8") == src.with_suffix(".jsonl").read_text(
        encoding="utf-8"
    )
    assert not dest.with_name(src.stem + ".events.jsonl").exists()
    assert report["events"][0]["sidecar"] == SIDECAR_OK
    assert report["sidecar"] == {"ok": 1, "missing": 0, "invalid_json": 0}


def test_flag_on_defective_jsonl_is_copied(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    broken = "{not-json\n"
    src.with_suffix(".jsonl").write_text(broken, encoding="utf-8")
    report = watch(source, root=tva_root, once=True, stable_s=0)
    sidecar = sidecar_dest_path(tva_root, src)
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == broken
    assert report["events"][0]["sidecar"] == SIDECAR_INVALID_JSON
    assert report["sidecar"] == {"ok": 0, "missing": 0, "invalid_json": 1}


def test_flag_on_events_jsonl_only_is_missing(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    _write_jsonl(src.with_name(src.stem + ".events.jsonl"), [{"event": "nope"}])
    report = watch(source, root=tva_root, once=True, stable_s=0)
    assert not sidecar_dest_path(tva_root, src).exists()
    assert not dest_path(tva_root, src).with_name(src.stem + ".events.jsonl").exists()
    assert report["events"][0]["sidecar"] == SIDECAR_MISSING


def test_ingest_flag_on_copies_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    src = write_video(tmp_path / "2026-09-11 14-30-00.mp4")
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "record_start"}])
    _write_jsonl(src.with_name(src.stem + ".events.jsonl"), [{"event": "nope"}])
    ingest(src, root=tva_root)
    sidecar = sidecar_dest_path(tva_root, src)
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == src.with_suffix(".jsonl").read_text(
        encoding="utf-8"
    )
    assert not (config.recordings_dir(tva_root) / (src.stem + ".events.jsonl")).exists()


def test_ingest_flag_off_does_not_copy_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_SIDECAR, raising=False)
    src = write_video(tmp_path / "2026-09-11 14-30-00.mp4")
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "record_start"}])
    ingest(src, root=tva_root)
    assert not sidecar_dest_path(tva_root, src).exists()


def test_ingest_split_parts_copy_each_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    part1 = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    part2 = write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0)
    _write_jsonl(part1.with_suffix(".jsonl"), [{"event": "start"}])
    _write_jsonl(part2.with_suffix(".jsonl"), [{"event": "split"}])
    ingest(part2, root=tva_root)
    assert sidecar_dest_path(tva_root, part1).is_file()
    assert sidecar_dest_path(tva_root, part2).is_file()


def test_flag_off_already_ingested_does_not_attach_sidecar(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    first = watch(source, root=tva_root, once=True, stable_s=0)
    assert first["events"][0]["action"] == "copied"
    assert "sidecar" not in first
    assert "sidecar" not in first["events"][0]
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "late"}])
    second = watch(source, root=tva_root, once=True, stable_s=0)
    assert second["events"][0]["reason"] == "ingested"
    assert "sidecar" not in second
    assert "sidecar" not in second["events"][0]
    assert not sidecar_dest_path(tva_root, src).exists()


def test_ingest_attaches_late_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    src = write_video(tmp_path / "2026-09-11 14-30-00.mp4")
    ingest(src, root=tva_root)
    assert not sidecar_dest_path(tva_root, src).exists()
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "late"}])
    ingest(src, root=tva_root)
    sidecar = sidecar_dest_path(tva_root, src)
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == src.with_suffix(".jsonl").read_text(
        encoding="utf-8"
    )


def test_watch_attaches_late_sidecar_when_already_ingested(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    first = watch(source, root=tva_root, once=True, stable_s=0)
    assert first["events"][0]["action"] == "copied"
    assert first["events"][0]["sidecar"] == SIDECAR_MISSING
    assert not sidecar_dest_path(tva_root, src).exists()
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "late"}])
    second = watch(source, root=tva_root, once=True, stable_s=0)
    assert second["events"][0]["reason"] == "ingested"
    assert second["events"][0]["sidecar"] == SIDECAR_OK
    sidecar = sidecar_dest_path(tva_root, src)
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == src.with_suffix(".jsonl").read_text(
        encoding="utf-8"
    )


def test_watch_does_not_overwrite_existing_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "first"}])
    watch(source, root=tva_root, once=True, stable_s=0)
    sidecar = sidecar_dest_path(tva_root, src)
    first = sidecar.read_text(encoding="utf-8")
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "second"}])
    report = watch(source, root=tva_root, once=True, stable_s=0)
    assert report["events"][0]["reason"] == "ingested"
    assert sidecar.read_text(encoding="utf-8") == first


def test_dest_exists_still_copies_sidecar(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    dest = dest_path(tva_root, src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "late"}])
    report = watch(source, root=tva_root, once=True, stable_s=0)
    assert report["events"][0]["reason"] == "dest-exists"
    assert report["events"][0]["sidecar"] == SIDECAR_OK
    assert sidecar_dest_path(tva_root, src).is_file()


def test_cli_watch_sidecar_counts(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv(ENV_SIDECAR, "1")
    source = tmp_path / "obs"
    source.mkdir()
    src = write_video(_obs_name(source))
    _write_jsonl(src.with_suffix(".jsonl"), [{"event": "record_start"}])
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
    assert payload["sidecar"] == {"ok": 1, "missing": 0, "invalid_json": 0}


def test_process_source_flag_off_event_keys_unchanged(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_SIDECAR, raising=False)
    source = tmp_path / "obs"
    source.mkdir()
    write_video(_obs_name(source))
    events = process_source(source, root=tva_root, stable_s=0)
    assert "sidecar" not in events[0]


def test_python_never_hardcodes_nas_sidecar_dest() -> None:
    """Sidecar dest comes from TVA_ROOT / --root. No NAS drive or UNC in src."""
    forbidden = ("T:\\tradevid", "Z:\\tradevid", "AHORNBUSINESS", r"\\AHORNBUSINESS")
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path}: {token!r}"
