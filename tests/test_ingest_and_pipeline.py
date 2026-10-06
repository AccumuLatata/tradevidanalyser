import shutil
from pathlib import Path

import pytest

from tradevidanalyser import config, media, store
from tradevidanalyser.cli import main
from tradevidanalyser.flags import (
    ENV_ALLOW_SESSION_ID_OVERWRITE,
    allow_session_id_overwrite,
)
from tradevidanalyser.ingest import SESSION_ID_COLLISION_ERROR, IngestError, ingest
from tradevidanalyser.pipeline import extract_session, run_latest, transcribe_session


def test_ingest_is_idempotent(tva_root: Path, sample_video: Path) -> None:
    first = ingest(sample_video, root=tva_root)
    second = ingest(sample_video, root=tva_root)
    assert first.id == "2026-09-11_143000"
    assert first.recording.sha256 == second.recording.sha256
    assert first.recording.duration_s > 0
    assert first.recording.parts == []
    assert store.session_json_path(tva_root, first.id).is_file()
    assert store.audio_path(tva_root, first.id).is_file()
    assert not store.desktop_audio_path(tva_root, first.id).is_file()
    raw = store.session_json_path(tva_root, first.id).read_text(encoding="utf-8")
    ingest(sample_video, root=tva_root)
    assert store.session_json_path(tva_root, first.id).read_text(encoding="utf-8") == raw


def test_fake_transcribe_and_cited_extract(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcript = transcribe_session(record.id, root=tva_root, provider_name="fake")
    assert transcript.segments
    assert transcript.segments[0].lang == "de"
    insights = extract_session(record.id, root=tva_root, provider_name="fake")
    assert insights.stated_levels
    assert any(span.token == "ONH" for span in insights.stated_levels)
    assert insights.bias_statements
    for span in insights.stated_levels:
        seg = next(s for s in transcript.segments if s.id == span.seg)
        assert span.text in seg.text


def test_run_latest(tva_root: Path, sample_video: Path) -> None:
    record = run_latest(tva_root, video=sample_video)
    status = store.compute_status(tva_root, record.id)
    assert status.stages["ingest"] == "ok"
    assert status.stages["transcribe"] == "ok"
    assert status.stages["extract"] == "ok"


def test_two_sessions_same_day_do_not_collide(tva_root: Path, tmp_path: Path, write_video) -> None:
    a = write_video(tmp_path / "2026-09-11 10-00-00.mp4")
    b = write_video(tmp_path / "2026-09-11 16-00-00.mp4")
    first = ingest(a, root=tva_root)
    second = ingest(b, root=tva_root)
    assert first.id != second.id
    assert store.list_session_ids(tva_root) == [first.id, second.id]
    assert store.latest_session_id(tva_root) == second.id
    assert first.recording.parts == []
    assert second.recording.parts == []


def test_split_parts_become_one_session(tva_root: Path, tmp_path: Path, write_video) -> None:
    part1 = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    part2 = write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0)
    record = ingest(part2, root=tva_root)
    assert record.id == "2026-09-11_143000"
    assert store.list_session_ids(tva_root) == [record.id]
    assert len(record.recording.parts) == 2
    assert record.recording.parts[0].offset_s == 0.0
    assert record.recording.parts[0].filename == part1.name
    assert record.recording.parts[1].filename == part2.name
    assert record.recording.parts[1].offset_s == pytest.approx(
        record.recording.parts[0].duration_s, abs=0.05
    )
    assert record.recording.duration_s == pytest.approx(
        record.recording.parts[0].duration_s + record.recording.parts[1].duration_s,
        abs=0.05,
    )
    mic = store.audio_path(tva_root, record.id)
    assert mic.is_file()
    assert media.duration_s(media.ffprobe(mic)) == pytest.approx(
        record.recording.duration_s, abs=0.35
    )
    again = ingest(part1, root=tva_root)
    assert again.id == record.id
    assert len(again.recording.parts) == 2
    raw = store.session_json_path(tva_root, record.id).read_text(encoding="utf-8")
    ingest(part2, root=tva_root)
    assert store.session_json_path(tva_root, record.id).read_text(encoding="utf-8") == raw


def test_split_chapter_times_shifted_by_part_offset(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=2.0,
        chapters=[(0.0, "open")],
    )
    write_video(
        tmp_path / "2026-09-11 14-30-02.mp4",
        seconds=2.0,
        chapters=[(0.0, "second")],
    )
    record = ingest(tmp_path / "2026-09-11 14-30-00.mp4", root=tva_root)
    names = [c.name for c in record.recording.chapters]
    assert "open" in names
    assert "second" in names
    by_name = {c.name: c.t for c in record.recording.chapters}
    assert by_name["open"] == pytest.approx(0.0, abs=0.05)
    assert by_name["second"] == pytest.approx(record.recording.parts[1].offset_s, abs=0.05)
    assert by_name["second"] == pytest.approx(record.recording.parts[0].duration_s, abs=0.05)


def test_desktop_track_writes_opus_and_is_not_transcribed(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=1.0,
        extra_audio=True,
    )
    record = ingest(video, root=tva_root, desktop_track=True)
    desk = store.desktop_audio_path(tva_root, record.id)
    mic = store.audio_path(tva_root, record.id)
    assert desk.is_file()
    assert mic.is_file()
    assert "desktop" in record.recording.tracks
    transcript = transcribe_session(record.id, root=tva_root, provider_name="fake")
    assert transcript.segments
    assert store.transcript_path(tva_root, record.id).is_file()


def test_extract_track_selects_audio_index(tmp_path: Path, write_video) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=1.0,
        extra_audio=True,
    )
    mic = tmp_path / "mic.opus"
    desk = tmp_path / "desk.opus"
    media.extract_track(video, 0, mic)
    media.extract_track(video, 1, desk)
    assert mic.is_file() and desk.is_file()
    assert media.sha256_file(mic) != media.sha256_file(desk)


def test_different_filename_prefixes_do_not_stitch(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    a = write_video(tmp_path / "NY 2026-09-11 14-30-00.mp4", seconds=2.0)
    b = write_video(tmp_path / "ES 2026-09-11 14-30-02.mp4", seconds=2.0)
    first = ingest(a, root=tva_root)
    second = ingest(b, root=tva_root)
    assert first.id != second.id
    assert first.recording.parts == []
    assert second.recording.parts == []
    assert store.list_session_ids(tva_root) == sorted([first.id, second.id])


def test_desktop_track_on_reingest_extracts_when_missing(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(
        tmp_path / "2026-09-11 14-30-00.mp4",
        seconds=1.0,
        extra_audio=True,
    )
    first = ingest(video, root=tva_root)
    assert not store.desktop_audio_path(tva_root, first.id).is_file()
    second = ingest(video, root=tva_root, desktop_track=True)
    assert store.desktop_audio_path(tva_root, second.id).is_file()
    assert "desktop" in second.recording.tracks


def test_desktop_track_without_second_audio_does_not_claim_desktop(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    video = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=1.0)
    record = ingest(video, root=tva_root, desktop_track=True)
    assert not store.desktop_audio_path(tva_root, record.id).is_file()
    assert "desktop" not in record.recording.tracks
    assert store.audio_path(tva_root, record.id).is_file()


def test_new_split_part_invalidates_transcript(tva_root: Path, tmp_path: Path, write_video) -> None:
    part1 = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    record = ingest(part1, root=tva_root)
    transcribe_session(record.id, root=tva_root, provider_name="fake")
    extract_session(record.id, root=tva_root, provider_name="fake")
    assert store.transcript_path(tva_root, record.id).is_file()
    assert store.insights_path(tva_root, record.id).is_file()
    write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0)
    updated = ingest(part1, root=tva_root)
    assert len(updated.recording.parts) == 2
    assert not store.transcript_path(tva_root, record.id).is_file()
    assert not store.insights_path(tva_root, record.id).is_file()
    status = store.compute_status(tva_root, record.id)
    assert status.stages["transcribe"] == "missing"
    assert status.stages["extract"] == "missing"


def test_unreadable_sibling_does_not_block_ingest(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    good = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    (tmp_path / "2026-09-11 14-30-02.mp4").write_bytes(b"not a video")
    record = ingest(good, root=tva_root)
    assert record.id == "2026-09-11_143000"
    assert record.recording.parts == []
    assert store.audio_path(tva_root, record.id).is_file()


def test_reingest_restores_missing_mic(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    store.audio_path(tva_root, record.id).unlink()
    again = ingest(sample_video, root=tva_root)
    assert again.id == record.id
    assert store.audio_path(tva_root, again.id).is_file()


def test_later_part_alone_then_stitch_drops_orphan(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    part2 = write_video(tmp_path / "2026-09-11 14-30-02.mp4", seconds=2.0)
    orphan = ingest(part2, root=tva_root)
    assert orphan.id == "2026-09-11_143002"
    write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=2.0)
    stitched = ingest(tmp_path / "2026-09-11 14-30-00.mp4", root=tva_root)
    assert stitched.id == "2026-09-11_143000"
    assert len(stitched.recording.parts) == 2
    assert store.list_session_ids(tva_root) == [stitched.id]


def test_overwrite_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ALLOW_SESSION_ID_OVERWRITE, raising=False)
    assert allow_session_id_overwrite() is False
    for raw in ("", "0", "false", "off", "no"):
        monkeypatch.setenv(ENV_ALLOW_SESSION_ID_OVERWRITE, raw)
        assert allow_session_id_overwrite() is False


def test_m8_same_start_different_prefix_is_collision(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    first_src = write_video(tmp_path / "NY 2026-09-11 14-30-00.mp4", seconds=1.0)
    second_src = write_video(tmp_path / "ES 2026-09-11 14-30-00.mp4", seconds=2.0)
    first = ingest(first_src, root=tva_root)
    first_json = store.session_json_path(tva_root, first.id).read_text(encoding="utf-8")
    first_sha = first.recording.sha256
    recordings = config.recordings_dir(tva_root)
    assert (recordings / first_src.name).is_file()
    with pytest.raises(IngestError, match=SESSION_ID_COLLISION_ERROR) as exc:
        ingest(second_src, root=tva_root)
    assert first.id in str(exc.value)
    assert store.session_json_path(tva_root, first.id).read_text(encoding="utf-8") == first_json
    again = store.load_session(tva_root, first.id)
    assert again.recording.sha256 == first_sha
    assert again.recording.filename == first_src.name
    assert (recordings / first_src.name).is_file()
    assert not (recordings / second_src.name).is_file()
    assert store.list_session_ids(tva_root) == [first.id]
    assert main(["--root", str(tva_root), "ingest", str(second_src)]) == 1


def test_m8_overwrite_flag_keeps_today_overwrite(
    tva_root: Path, tmp_path: Path, write_video, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_ALLOW_SESSION_ID_OVERWRITE, "1")
    first_src = write_video(tmp_path / "NY 2026-09-11 14-30-00.mp4", seconds=1.0)
    second_src = write_video(tmp_path / "ES 2026-09-11 14-30-00.mp4", seconds=2.0)
    first = ingest(first_src, root=tva_root)
    second = ingest(second_src, root=tva_root)
    assert second.id == first.id
    assert second.recording.filename == second_src.name
    assert second.recording.sha256 != first.recording.sha256
    assert (config.recordings_dir(tva_root) / second_src.name).is_file()


def test_same_filename_other_sha_overwrites_with_message(
    tva_root: Path, tmp_path: Path, write_video, capsys
) -> None:
    path = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=1.0)
    first = ingest(path, root=tva_root)
    write_video(path, seconds=2.0)
    second = ingest(path, root=tva_root)
    assert second.id == first.id
    assert second.recording.filename == path.name
    assert second.recording.sha256 != first.recording.sha256
    err = capsys.readouterr().err
    assert first.id in err
    assert "andere SHA" in err


def test_empty_stored_filename_other_sha_is_collision(
    tva_root: Path, tmp_path: Path, write_video
) -> None:
    first_src = write_video(tmp_path / "2026-09-11 14-30-00.mp4", seconds=1.0)
    first = ingest(first_src, root=tva_root)
    payload = store.read_json(store.session_json_path(tva_root, first.id))
    payload["recording"]["filename"] = ""
    store.write_json(store.session_json_path(tva_root, first.id), payload)
    other = write_video(tmp_path / "NY 2026-09-11 14-30-00.mp4", seconds=2.0)
    with pytest.raises(IngestError, match=SESSION_ID_COLLISION_ERROR):
        ingest(other, root=tva_root)
    assert store.load_session(tva_root, first.id).recording.sha256 == first.recording.sha256
    assert not (config.recordings_dir(tva_root) / other.name).is_file()


def test_l0_session_identity_with_overwrite_flag(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_ALLOW_SESSION_ID_OVERWRITE, "1")
    src = Path(__file__).parent / "fixtures" / "l0_main_f44d864" / "l0-ocr" / "session.json"
    dest = config.session_dir(tva_root, "2026-05-14_160300")
    dest.mkdir(parents=True)
    shutil.copy2(src, dest / "session.json")
    record = store.load_session(tva_root, "2026-05-14_160300")
    assert record.id == "2026-05-14_160300"
    assert (dest / "session.json").read_bytes() == src.read_bytes()
    assert not (tva_root / "days").exists()
