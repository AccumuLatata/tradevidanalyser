from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, run_latest, transcribe_session


def test_ingest_is_idempotent(tva_root: Path, sample_video: Path) -> None:
    first = ingest(sample_video, root=tva_root)
    second = ingest(sample_video, root=tva_root)
    assert first.id == "2026-09-11_143000"
    assert first.recording.sha256 == second.recording.sha256
    assert first.recording.duration_s > 0
    assert store.session_json_path(tva_root, first.id).is_file()
    assert store.audio_path(tva_root, first.id).is_file()
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
