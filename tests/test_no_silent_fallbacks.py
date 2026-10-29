"""PR-43: unset providers fail loudly; fake is explicit; no silent fallbacks."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.day_publish import FAKE_FACTS_PUBLISH_ERROR, refuse_fake_fact_providers
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.evidence import DEFAULT_POST_S, DEFAULT_PRE_S, evidence_session
from tradevidanalyser.ingest import AUDIO_FAILED_PREFIX, AUDIO_STAGE, IngestError, ingest
from tradevidanalyser.media import MediaError
from tradevidanalyser.ocr import OcrError, get_ocr_provider
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.extract import ExtractError, get_extract_provider
from tradevidanalyser.provider_resolve import (
    ENV_ASR_PROVIDER,
    ENV_EXTRACT_PROVIDER,
    ENV_OCR_PROVIDER,
    fake_provider_warning,
    missing_provider_error,
)
from tradevidanalyser.publish import PublishError
from tradevidanalyser.schema import (
    Alignment,
    Evidence,
    EvidenceWindow,
    RecordingInfo,
    SessionRecord,
)
from tradevidanalyser.serve import create_app

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pr43"
CLIPPED_WINDOW = json.loads((FIXTURE_DIR / "window_clipped.json").read_text(encoding="utf-8"))
AUDIO_FAILED = json.loads((FIXTURE_DIR / "audio_failed_status.json").read_text(encoding="utf-8"))
FAKE_EVIDENCE = json.loads(
    (FIXTURE_DIR / "fake_provider_evidence.json").read_text(encoding="utf-8")
)
VIENNA = timezone(timedelta(hours=2))


def _unset_n4_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ASR_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_OCR_PROVIDER, raising=False)
    monkeypatch.delenv(ENV_EXTRACT_PROVIDER, raising=False)


def test_unset_asr_provider_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_ASR_PROVIDER, raising=False)
    with pytest.raises(AsrError, match=missing_provider_error(ENV_ASR_PROVIDER)):
        get_asr_provider()
    monkeypatch.setenv(ENV_ASR_PROVIDER, "  ")
    with pytest.raises(AsrError, match=missing_provider_error(ENV_ASR_PROVIDER)):
        get_asr_provider()
    assert get_asr_provider("whisperx").name == "whisperx"


def test_explicit_fake_asr_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_ASR_PROVIDER, "fake")
    with pytest.warns(UserWarning, match=re.escape(fake_provider_warning(ENV_ASR_PROVIDER, "fake"))):
        assert get_asr_provider().name == "fake"
    with pytest.warns(UserWarning, match=re.escape(fake_provider_warning(ENV_ASR_PROVIDER, "test"))):
        assert get_asr_provider("test").name == "fake"


def test_unset_ocr_provider_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_OCR_PROVIDER, raising=False)
    with pytest.raises(OcrError, match=missing_provider_error(ENV_OCR_PROVIDER)):
        get_ocr_provider()


def test_explicit_fake_ocr_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_OCR_PROVIDER, "fake")
    with pytest.warns(UserWarning, match=re.escape(fake_provider_warning(ENV_OCR_PROVIDER, "fake"))):
        assert get_ocr_provider().name == "fake"


def test_unset_extract_provider_is_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_EXTRACT_PROVIDER, raising=False)
    with pytest.raises(ExtractError, match=missing_provider_error(ENV_EXTRACT_PROVIDER)):
        get_extract_provider()
    assert get_extract_provider("grok").name == "grok"


def test_explicit_fake_extract_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_EXTRACT_PROVIDER, "fake")
    with pytest.warns(UserWarning, match=re.escape(fake_provider_warning(ENV_EXTRACT_PROVIDER, "fake"))):
        assert get_extract_provider().name == "fake"
    with pytest.warns(UserWarning, match=re.escape(fake_provider_warning(ENV_EXTRACT_PROVIDER, "keyword"))):
        assert get_extract_provider("keyword").name == "fake"


def test_cli_transcribe_unset_provider_exits_nonzero(
    tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    record = ingest(sample_video, root=tva_root)
    monkeypatch.delenv(ENV_ASR_PROVIDER, raising=False)
    assert main(["--root", str(tva_root), "transcribe", record.id]) == 1
    assert missing_provider_error(ENV_ASR_PROVIDER) in capsys.readouterr().out
    assert not store.transcript_path(tva_root, record.id).is_file()


def test_media_error_sets_audio_failed_and_raises(
    tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args, **kwargs):
        raise MediaError("ffmpeg extract failed")

    monkeypatch.setattr("tradevidanalyser.media.extract_track", boom)
    with pytest.raises(IngestError, match=AUDIO_FAILED_PREFIX) as exc:
        ingest(sample_video, root=tva_root)
    assert "ffmpeg extract failed" in str(exc.value)
    session_id = "2026-09-11_143000"
    assert store.session_json_path(tva_root, session_id).is_file()
    status = store.compute_status(tva_root, session_id)
    assert status.stages[AUDIO_STAGE] == AUDIO_FAILED["stages"]["audio"]
    assert status.error is not None
    assert status.error.startswith(AUDIO_FAILED["error_prefix"])
    raw = store.read_json(store.session_json_path(tva_root, session_id))
    assert raw["id"] == session_id
    assert "audio_error" not in raw


def _write_trades(root: Path, session_id: str, rows: list[tuple[str, float, float]]) -> None:
    start = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)
    ids = [row[0] for row in rows]
    entries = [start + timedelta(seconds=row[1]) for row in rows]
    exits = [start + timedelta(seconds=row[2]) for row in rows]
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


def test_window_clipped_fields_match_fixture(tva_root: Path) -> None:
    record = SessionRecord(
        id="2026-09-11_143000",
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA).isoformat(),
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
        ),
        alignment=Alignment(
            offset_s=0.0,
            drift_s_per_h=0.0,
            confidence=0.96,
            method="filename",
        ),
    )
    store.save_session(tva_root, record)
    _write_trades(tva_root, record.id, [("T01", 4000.0, 4060.0)])
    result = evidence_session(record.id, root=tva_root, provider_name="fake")
    assert result.status == "ok"
    evidence = Evidence.model_validate(store.read_json(store.evidence_path(tva_root, record.id)))
    window = evidence.trades[0].window.model_dump(mode="json")
    assert window == CLIPPED_WINDOW
    raw_t0 = 4000.0 - DEFAULT_PRE_S
    raw_t1 = 4060.0 + DEFAULT_POST_S
    assert window["t0_raw"] == pytest.approx(raw_t0)
    assert window["t1_raw"] == pytest.approx(raw_t1)


def test_unclipped_window_omits_clipped_fields() -> None:
    payload = EvidenceWindow(t0=20.0, t1=380.0).model_dump(mode="json")
    assert payload == {"t0": 20.0, "t1": 380.0}
    assert "window_clipped" not in payload


def test_day_publish_refuses_fake_evidence(tva_root: Path) -> None:
    record = SessionRecord(
        id="2026-09-14_090000",
        recording=RecordingInfo(
            path="recordings/2026-09-14 09-00-00.mp4",
            sha256="a" * 64,
            start_wallclock_vienna=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA).isoformat(),
            duration_s=600.0,
            filename="2026-09-14 09-00-00.mp4",
        ),
    )
    store.save_session(tva_root, record)
    store.write_json(store.evidence_path(tva_root, record.id), FAKE_EVIDENCE)
    with pytest.raises(PublishError, match=FAKE_FACTS_PUBLISH_ERROR):
        refuse_fake_fact_providers(tva_root, [record])


def test_serve_run_unset_asr_returns_error(
    tva_root: Path, sample_video: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record = ingest(sample_video, root=tva_root)
    monkeypatch.delenv(ENV_ASR_PROVIDER, raising=False)
    client = TestClient(create_app(tva_root))
    posted = client.post(f"/sessions/{record.id}/run", params={"stages": "transcribe"})
    assert posted.status_code == 400
    assert missing_provider_error(ENV_ASR_PROVIDER) in posted.json()["detail"]
    assert not store.transcript_path(tva_root, record.id).is_file()


def test_doctor_unset_providers_warn(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _unset_n4_providers(monkeypatch)
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["asr_provider"].status == "warn"
    assert ids["asr_provider"].detail == missing_provider_error(ENV_ASR_PROVIDER)
    assert ids["extract_provider"].status == "warn"
    assert ids["extract_provider"].detail == missing_provider_error(ENV_EXTRACT_PROVIDER)
    assert ids["ocr_provider"].status == "warn"
    assert ids["ocr_provider"].detail == missing_provider_error(ENV_OCR_PROVIDER)
    assert report.ok
