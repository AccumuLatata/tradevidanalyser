from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.providers.asr import AsrError, get_asr_provider
from tradevidanalyser.providers.asr_hosted import (
    DEEPGRAM_LISTEN_URL,
    DEEPGRAM_USD_PER_AUDIO_HOUR,
    DeepgramAsrProvider,
    ScribeAsrProvider,
    estimate_cost_usd,
    glossary_keyterms,
    transcript_from_deepgram,
)
from tradevidanalyser import store

FIXTURE = Path(__file__).parent / "fixtures" / "deepgram_listen.json"


def _payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_deepgram_mapping_from_committed_fixture() -> None:
    transcript = transcript_from_deepgram(
        _payload(), language="de", model="nova-3", prompt_version="jargon-v1+test"
    )
    assert transcript.provider == "deepgram"
    assert transcript.language == "de"
    assert [seg.id for seg in transcript.segments] == ["seg_001", "seg_002"]
    assert transcript.segments[0].text == "Bias long."
    assert transcript.segments[1].words[1].w == "ONH"
    assert transcript.segments[0].lang == "de"


def test_get_asr_provider_hosted_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TVA_ASR_PROVIDER", raising=False)
    assert get_asr_provider().name == "fake"
    assert get_asr_provider("deepgram").name == "deepgram"
    assert get_asr_provider("hosted").name == "deepgram"
    assert get_asr_provider("scribe").name == "scribe"


def test_scribe_is_reserved_interface() -> None:
    with pytest.raises(AsrError, match="deepgram"):
        ScribeAsrProvider().transcribe(Path("audio/mic.opus"), language="de")


def test_deepgram_http_mock_uploads_audio_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    audio = tmp_path / "mic.opus"
    audio_bytes = b"OggS-not-really-opus-but-audio-only"
    audio.write_bytes(audio_bytes)
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_payload())

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")
    client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = DeepgramAsrProvider(client=client)
    transcript = provider.transcribe(audio, language="de")

    assert len(captured) == 1
    request = captured[0]
    assert str(request.url).startswith(DEEPGRAM_LISTEN_URL)
    assert request.method == "POST"
    assert request.content == audio_bytes
    assert request.headers["content-type"].startswith("audio/")
    assert request.headers["authorization"] == "Token dg-test-key"
    assert "language=de" in str(request.url)
    assert "keyterm=ONH" in str(request.url)
    assert "dVWAP" in str(request.url)
    assert "video" not in str(request.url).lower()
    assert "frame" not in str(request.url).lower()
    assert b"mp4" not in request.content
    assert transcript.segments[0].id == "seg_001"
    assert provider.last_cost_usd == estimate_cost_usd(5.2)
    assert provider.last_cost_usd == round(5.2 / 3600 * DEEPGRAM_USD_PER_AUDIO_HOUR, 6)


def test_deepgram_refuses_video(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    video = tmp_path / "tape.mp4"
    video.write_bytes(b"not-audio")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not upload video: {request.url}")

    provider = DeepgramAsrProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AsrError, match="audio only"):
        provider.transcribe(video, language="de")


def test_deepgram_refuses_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    frame = tmp_path / "chapter.jpg"
    frame.write_bytes(b"\xff\xd8not-a-real-jpeg")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not upload frames: {request.url}")

    provider = DeepgramAsrProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AsrError, match="frames"):
        provider.transcribe(frame, language="de")


def test_deepgram_refuses_unknown_suffix(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    mystery = tmp_path / "mic.bin"
    mystery.write_bytes(b"not-audio")
    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"must not upload unknown media: {request.url}")

    provider = DeepgramAsrProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(AsrError, match="audio only"):
        provider.transcribe(mystery, language="de")


def test_hosted_http_never_called_when_provider_is_fake(
    monkeypatch: pytest.MonkeyPatch, tva_root: Path, sample_video: Path
) -> None:
    def fail_send(self, request, *args, **kwargs):
        raise AssertionError(f"hosted HTTP must not run for fake: {request.url}")

    monkeypatch.setattr(httpx.Client, "send", fail_send)
    monkeypatch.delenv("TVA_ASR_PROVIDER", raising=False)
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root, provider_name="fake")
    extract_session(record.id, root=tva_root, provider_name="fake")


def test_pipeline_logs_cost_usd(
    monkeypatch: pytest.MonkeyPatch, tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    audio = store.audio_path(tva_root, record.id)
    assert audio.is_file()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.content == audio.read_bytes()
        return httpx.Response(200, json=_payload())

    monkeypatch.setenv("DEEPGRAM_API_KEY", "dg-test-key")
    provider = DeepgramAsrProvider(client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(
        "tradevidanalyser.pipeline.get_asr_provider",
        lambda name=None: provider,
    )
    transcribe_session(record.id, root=tva_root, provider_name="deepgram")
    status = store.compute_status(tva_root, record.id)
    assert status.cost_usd == estimate_cost_usd(5.2)
    extract_session(record.id, root=tva_root)
    assert store.compute_status(tva_root, record.id).cost_usd == estimate_cost_usd(5.2)


def test_deepgram_requires_api_key(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"x")
    with pytest.raises(AsrError, match="DEEPGRAM_API_KEY"):
        DeepgramAsrProvider().transcribe(audio)


def test_deepgram_maps_channels_when_utterances_missing() -> None:
    payload = {
        "metadata": {"duration": 2.3},
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "Bias long.",
                            "words": [
                                {
                                    "word": "bias",
                                    "start": 0.0,
                                    "end": 0.4,
                                    "punctuated_word": "Bias",
                                },
                                {
                                    "word": "long",
                                    "start": 0.4,
                                    "end": 0.8,
                                    "punctuated_word": "long.",
                                },
                            ],
                        }
                    ]
                }
            ]
        },
    }
    transcript = transcript_from_deepgram(
        payload, language="de", model="nova-3", prompt_version="jargon-v1+test"
    )
    assert [seg.id for seg in transcript.segments] == ["seg_001"]
    assert transcript.segments[0].text == "Bias long."
    assert transcript.segments[0].words[0].w == "Bias"


def test_deepgram_rebuilds_text_from_words_and_skips_bad_items() -> None:
    payload = {
        "results": {
            "utterances": [
                {
                    "start": float("nan"),
                    "end": None,
                    "transcript": "",
                    "words": [
                        "not-a-word-object",
                        {
                            "word": "onh",
                            "start": 1.5,
                            "end": 1.9,
                            "confidence": "nope",
                            "punctuated_word": "ONH",
                        },
                        {"word": "", "start": 2.0, "end": 2.1},
                    ],
                }
            ]
        }
    }
    transcript = transcript_from_deepgram(
        payload, language="de", model="nova-3", prompt_version="jargon-v1+test"
    )
    assert transcript.segments[0].text == "ONH"
    assert transcript.segments[0].t0 == 1.5
    assert transcript.segments[0].t1 == 1.9
    assert transcript.segments[0].words[0].p == 1.0


def test_estimate_cost_usd_ignores_invalid_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TVA_ASR_USD_PER_HOUR", "not-a-number")
    assert estimate_cost_usd(3600.0) == DEEPGRAM_USD_PER_AUDIO_HOUR
    monkeypatch.setenv("TVA_ASR_USD_PER_HOUR", "nan")
    assert estimate_cost_usd(3600.0) == DEEPGRAM_USD_PER_AUDIO_HOUR
    assert estimate_cost_usd(float("nan")) == 0.0
    assert estimate_cost_usd(3600.0, usd_per_hour=-1.0) == DEEPGRAM_USD_PER_AUDIO_HOUR


def test_compute_status_survives_corrupt_previous_status(
    tva_root: Path, sample_video: Path
) -> None:
    record = ingest(sample_video, root=tva_root)
    path = store.status_path(tva_root, record.id)
    path.write_text("{not-json", encoding="utf-8")
    status = store.compute_status(tva_root, record.id, cost_usd=0.12)
    assert status.cost_usd == 0.12
    assert store.compute_status(tva_root, record.id).cost_usd == 0.12


def test_glossary_keyterms_include_onh() -> None:
    terms = glossary_keyterms()
    assert "ONH" in terms
    assert "dVWAP" in terms
    assert "3c" in terms
    assert terms == list(load_glossary().tokens)[:80]


def test_doctor_deepgram_key_fail_when_selected(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TVA_ASR_PROVIDER", "deepgram")
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["asr_provider"].status == "ok"
    assert ids["deepgram_key"].status == "fail"
    assert ids["elevenlabs_key"].status == "warn"
    assert not report.ok


def test_doctor_reports_hosted_keys(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    monkeypatch.delenv("TVA_ASR_PROVIDER", raising=False)
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["deepgram_key"].status == "warn"
    assert ids["elevenlabs_key"].status == "warn"
    assert report.ok
