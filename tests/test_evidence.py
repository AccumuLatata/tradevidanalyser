from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.evidence import (
    ALIGNMENT_LOW,
    DEFAULT_POST_S,
    DEFAULT_PRE_S,
    evidence_session,
    segments_in_window,
    wall_to_video_t,
)
from tradevidanalyser.ingest import ingest
from tradevidanalyser.ocr import OcrRow, write_ocr_parquet
from tradevidanalyser.pipeline import _assert_citations, extract_session, transcribe_session
from tradevidanalyser.providers.extract import (
    NO_SPEECH_GAP,
    FakeExtractProvider,
    GrokExtractProvider,
    apply_stated_citation_guard,
    evidence_citation_problems,
    stated_from_chat_payload,
)
from tradevidanalyser.schema import (
    Alignment,
    Chapter,
    Evidence,
    EvidenceTrade,
    EvidenceWindow,
    RecordingInfo,
    SessionRecord,
    StatedCite,
    StatedFields,
    Transcript,
    TranscriptSegment,
)
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

VIENNA = timezone(timedelta(hours=2))
START = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)


def _session(
    root: Path,
    *,
    session_id: str = "2026-09-11_143000",
    start: datetime = START,
    duration_s: float = 3600.0,
    confidence: float = 0.96,
    offset_s: float = 0.0,
    drift_s_per_h: float = 0.0,
    chapters: list[Chapter] | None = None,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename="2026-09-11 14-30-00.mp4",
            chapters=chapters or [],
        ),
        alignment=Alignment(
            offset_s=offset_s,
            drift_s_per_h=drift_s_per_h,
            confidence=confidence,
            method="filename",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _video_to_wall(video_t: float, *, offset_s: float = 0.0, drift_s_per_h: float = 0.0) -> datetime:
    return START + timedelta(seconds=video_t + offset_s + drift_s_per_h * (video_t / 3600.0))


def _write_trades(
    root: Path,
    session_id: str,
    rows: list[tuple[str, float, float | None]],
    *,
    offset_s: float = 0.0,
    drift_s_per_h: float = 0.0,
) -> None:
    ids = [row[0] for row in rows]
    entries = [_video_to_wall(row[1], offset_s=offset_s, drift_s_per_h=drift_s_per_h) for row in rows]
    exits = [
        None
        if row[2] is None
        else _video_to_wall(row[2], offset_s=offset_s, drift_s_per_h=drift_s_per_h)
        for row in rows
    ]
    table = pa.table(
        {
            "tva_trade_id": pa.array(ids, type=pa.string()),
            "entry_timestamp": pa.array(entries, type=pa.timestamp("us", tz="UTC")),
            "exit_timestamp": pa.array(exits, type=pa.timestamp("us", tz="UTC")),
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _write_transcript(root: Path, session_id: str, segments: list[TranscriptSegment]) -> Transcript:
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        language="de",
        segments=segments,
    )
    store.save_transcript(root, session_id, transcript)
    return transcript


def _seg(i: int, t0: float, t1: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(id=f"seg_{i:03d}", t0=t0, t1=t1, lang="de", text=text)


def test_wall_to_video_inverts_offset_and_drift() -> None:
    offset = 1.8
    drift = 0.4
    video_t = 1800.0
    wall = _video_to_wall(video_t, offset_s=offset, drift_s_per_h=drift)
    assert wall_to_video_t(wall, START, offset_s=offset, drift_s_per_h=drift) == pytest.approx(
        video_t, abs=1e-6
    )


def test_window_boundaries_include_overlap_only() -> None:
    segs = [
        _seg(1, 0.0, 19.9, "ausserhalb"),
        _seg(2, 20.0, 30.0, "innen"),
        _seg(3, 380.0, 390.0, "am ende"),
        _seg(4, 381.0, 400.0, "danach"),
    ]
    inside = segments_in_window(segs, 20.0, 380.0)
    assert [seg.id for seg in inside] == ["seg_002", "seg_003"]


def test_fake_extractor_window_and_stated(tva_root: Path, capsys) -> None:
    record = _session(tva_root, chapters=[Chapter(t=200.0, name="fill")])
    _write_trades(tva_root, record.id, [("T01", 200.0, 260.0)])
    _write_transcript(
        tva_root,
        record.id,
        [
            _seg(1, 0.0, 10.0, "Vor dem Fenster."),
            _seg(2, 190.0, 210.0, "Bias ist long. Playbook ONH Touch Scalp."),
            _seg(3, 220.0, 240.0, "Stop unter dem Level. Ziel am ONH."),
            _seg(4, 900.0, 910.0, "Viel spaeter."),
        ],
    )
    frames = store.frames_dir(tva_root, record.id)
    frames.mkdir(parents=True)
    (frames / "200.000.jpg").write_bytes(b"x")
    clips = store.clips_dir(tva_root, record.id)
    clips.mkdir(parents=True)
    (clips / "200.000.mp4").write_bytes(b"mp4")
    write_ocr_parquet(
        store.ocr_path(tva_root, record.id),
        [OcrRow(t=200.0, roi="clock", text="14:33:20", confidence=0.9, parsed=None)],
    )
    assert main(["--root", str(tva_root), "evidence", record.id]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    assert '"provider": "fake"' in out
    evidence = Evidence.model_validate(store.read_json(store.evidence_path(tva_root, record.id)))
    assert evidence.provider == "fake"
    assert len(evidence.trades) == 1
    trade = evidence.trades[0]
    assert trade.tva_trade_id == "T01"
    assert trade.window.t0 == pytest.approx(200.0 - DEFAULT_PRE_S)
    assert trade.window.t1 == pytest.approx(260.0 + DEFAULT_POST_S)
    assert trade.commentary == ["seg_002", "seg_003"]
    assert trade.stated.bias is not None
    assert trade.stated.bias.value.lower().startswith("bias")
    assert trade.stated.playbook is not None
    assert "ONH Touch" in trade.stated.playbook.value
    assert trade.stated.stop_raw is not None
    assert "Stop" in trade.stated.stop_raw.value
    assert trade.stated.target_raw is not None
    assert "Ziel" in trade.stated.target_raw.value
    assert trade.alignment is None
    assert trade.alignment_confidence >= ALIGNMENT_LOW
    assert trade.markers and trade.markers[0].name == "fill"
    assert "200.000.jpg" in trade.frames
    assert trade.ocr and trade.ocr[0].roi == "clock"
    assert trade.clip == "200.000.mp4"
    assert store.compute_status(tva_root, record.id).stages["evidence"] == "ok"


def test_alignment_below_0_8_flags_low(tva_root: Path) -> None:
    record = _session(tva_root, confidence=0.6)
    _write_trades(tva_root, record.id, [("T01", 200.0, 220.0)])
    _write_transcript(tva_root, record.id, [_seg(1, 190.0, 210.0, "Bias ist long.")])
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "ok"
    evidence = Evidence.model_validate(store.read_json(store.evidence_path(tva_root, record.id)))
    assert evidence.trades[0].alignment == "low"
    assert evidence.trades[0].alignment_confidence == 0.6


def test_trade_without_speech_stated_null_and_gap(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, [("T01", 200.0, 220.0), ("T02", 800.0, 820.0)])
    _write_transcript(
        tva_root,
        record.id,
        [
            _seg(1, 190.0, 210.0, "Bias ist long. Playbook ONH Touch Scalp."),
            _seg(2, 215.0, 230.0, "Stop unter dem Level. Ziel am ONH."),
        ],
    )
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "ok"
    evidence = Evidence.model_validate(store.read_json(store.evidence_path(tva_root, record.id)))
    by_id = {trade.tva_trade_id: trade for trade in evidence.trades}
    spoken = by_id["T01"]
    silent = by_id["T02"]
    assert spoken.stated.stop_raw is not None
    assert spoken.stated.target_raw is not None
    assert spoken.stated.playbook is not None
    assert silent.stated == StatedFields()
    assert NO_SPEECH_GAP in silent.gaps
    assert silent.commentary == []


def test_no_trades_omits_stage(tva_root: Path) -> None:
    record = _session(tva_root)
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert not store.evidence_path(tva_root, record.id).is_file()
    assert "evidence" not in store.compute_status(tva_root, record.id).stages


def test_evidence_omitted_until_run(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    status = store.compute_status(tva_root, record.id)
    assert "evidence" not in status.stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_evidence_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="evidence", error="evidence exploded")
    status = store.compute_status(tva_root, record.id)
    assert "evidence" not in status.stages
    assert status.error is None
    client = TestClient(create_app(tva_root))
    failed = client.get("/sessions", params={"status": "failed"}).json()["sessions"]
    assert record.id not in failed
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_evidence_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "evidence", "../outside"]) == 1
    assert main(["--root", str(tva_root), "evidence", "foo/bar"]) == 1


def test_evidence_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "evidence" not in ALLOWED_RUN_STAGES


def test_invalidate_downstream_drops_evidence(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, [("T01", 200.0, 220.0)])
    _write_transcript(tva_root, record.id, [_seg(1, 190.0, 210.0, "Bias ist long.")])
    evidence_session(record.id, root=tva_root)
    assert store.evidence_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.evidence_path(tva_root, record.id).is_file()
    assert "evidence" not in store.compute_status(tva_root, record.id).stages


def test_fabricated_stated_fails_assert_citations() -> None:
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        segments=[_seg(1, 0.0, 10.0, "Bias ist long.")],
    )
    evidence = Evidence(
        provider="fake",
        model="fake-v1",
        prompt_version="stated-keyword-v1",
        session_id="2026-09-11_143000",
        trades=[
            EvidenceTrade(
                tva_trade_id="T01",
                window=EvidenceWindow(t0=0.0, t1=10.0),
                commentary=["seg_001"],
                stated=StatedFields(bias=StatedCite(value="fabricated quote", seg="seg_001")),
                alignment_confidence=0.96,
            )
        ],
    )
    problems = evidence_citation_problems(transcript, evidence)
    assert problems
    with pytest.raises(ValueError, match="quote not found"):
        _assert_citations(transcript, evidence=evidence)


def test_grok_stated_fields_drops_fabricated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        body = {
            "bias": {"value": "Bias ist long.", "seg": "seg_001"},
            "stop_raw": {"value": "invented stop", "seg": "seg_001"},
            "setup": None,
            "target_raw": None,
            "playbook": None,
            "gaps": [],
        }
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": __import__("json").dumps(body)}}]},
        )

    provider = GrokExtractProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        segments=[_seg(1, 0.0, 10.0, "Bias ist long.")],
    )
    stated = provider.stated_fields(transcript)
    assert stated.bias is not None
    assert stated.bias.value == "Bias ist long."
    assert stated.stop_raw is None
    assert any("dropped stop_raw" in gap for gap in stated.gaps)


def test_apply_stated_guard_nulls_unknown_seg() -> None:
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        segments=[_seg(1, 0.0, 10.0, "Bias ist long.")],
    )
    from tradevidanalyser.providers.extract import StatedFieldsPass

    cleaned = apply_stated_citation_guard(
        transcript,
        StatedFieldsPass(bias=StatedCite(value="Bias ist long.", seg="seg_999")),
    )
    assert cleaned.bias is None
    assert cleaned.gaps


def test_fake_stated_fields_direct() -> None:
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        segments=[
            _seg(1, 0.0, 5.0, "Bias ist long. Playbook ONH Touch Scalp."),
            _seg(2, 5.0, 10.0, "Stop unter dem Level. Ziel am ONH."),
        ],
    )
    stated = FakeExtractProvider().stated_fields(transcript)
    assert stated.playbook is not None
    assert stated.stop_raw is not None
    assert stated.target_raw is not None


def test_stated_from_chat_payload_round_trip() -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        '{"setup": null, "bias": {"value": "long", "seg": "seg_001"},'
                        ' "stop_raw": null, "target_raw": null, "playbook": null, "gaps": []}'
                    )
                }
            }
        ]
    }
    stated = stated_from_chat_payload(payload)
    assert stated.bias is not None
    assert stated.bias.seg == "seg_001"


@pytest.mark.golden
def test_golden_planted_attach_to_right_synthetic_trade() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    planted_path = golden / "planted.yaml"
    transcript_path = golden / "transcript.json"
    if not planted_path.is_file():
        pytest.skip("golden planted.yaml absent")
    if not transcript_path.is_file():
        pytest.skip("golden transcript.json absent")
    planted: dict[str, str] = {}
    for line in planted_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        planted[key.strip()] = value.strip().strip("\"'")
    transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    needles = [planted.get("playbook"), planted.get("stop_raw"), planted.get("target_raw")]
    hit_t: float | None = None
    for seg in transcript.segments:
        blob = seg.text
        if any(needle and needle in blob for needle in needles if needle):
            hit_t = seg.t0
            break
    if hit_t is None:
        pytest.skip("planted stop/target/playbook not in golden transcript")
    decoy_t = 0.0 if hit_t > 400 else min(
        (transcript.segments[-1].t1 if transcript.segments else 800.0) + 50.0,
        10_000.0,
    )
    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        root = Path(tmp) / "store"
        config.ensure_layout(root)
        record = _session(root, duration_s=max(hit_t + 600.0, decoy_t + 600.0, 3600.0))
        _write_trades(root, record.id, [("T01", hit_t, hit_t + 30.0), ("T02", decoy_t, decoy_t + 20.0)])
        store.save_transcript(root, record.id, transcript)
        result = evidence_session(record.id, root=root)
        assert result.status == "ok"
        evidence = Evidence.model_validate(store.read_json(store.evidence_path(root, record.id)))
        by_id = {trade.tva_trade_id: trade for trade in evidence.trades}
        spoken = by_id["T01"]
        silent = by_id["T02"]
        hay = " ".join(
            filter(
                None,
                [
                    spoken.stated.setup.value if spoken.stated.setup else "",
                    spoken.stated.playbook.value if spoken.stated.playbook else "",
                    spoken.stated.stop_raw.value if spoken.stated.stop_raw else "",
                    spoken.stated.target_raw.value if spoken.stated.target_raw else "",
                    *(store.load_transcript(root, record.id).segments[0].text for _ in ()),
                ],
            )
        )
        commentary = " ".join(
            next(seg.text for seg in transcript.segments if seg.id == seg_id)
            for seg_id in spoken.commentary
        )
        hay = f"{hay} {commentary}"
        for key in ("playbook", "stop_raw", "target_raw"):
            needle = planted.get(key)
            if not needle:
                continue
            assert needle in hay, f"planted {key}={needle!r} not on T01"
        if hit_t > DEFAULT_PRE_S and abs(decoy_t - hit_t) > DEFAULT_PRE_S + DEFAULT_POST_S:
            assert silent.stated.playbook is None or (
                planted.get("playbook") or ""
            ) not in (silent.stated.playbook.value if silent.stated.playbook else "")
