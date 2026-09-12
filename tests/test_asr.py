from __future__ import annotations

import sys
from pathlib import Path

import pytest

from tradevidanalyser import config
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import transcribe_session
from tradevidanalyser.providers.asr import (
    DEFAULT_WHISPERX_MODEL,
    WORD_TIME_TOLERANCE_S,
    AsrError,
    WhisperXAsrProvider,
    get_asr_provider,
    resolve_batch_size,
    resolve_compute_type,
    resolve_device,
    transcript_from_whisperx,
    transcripts_close,
    whisperx_importable,
)
from tradevidanalyser.schema import Transcript, TranscriptSegment, TranscriptWord
from tradevidanalyser.wer import score_session


class _StubModel:
    def transcribe(self, audio, batch_size=16, language=None, **kwargs):
        return {
            "language": language or "de",
            "segments": [
                {"start": 0.0, "end": 2.1, "text": "Bias long am ONH.", "language": "de"},
                {"start": 2.1, "end": 4.0, "text": "Playbook 3c.", "language": "en"},
            ],
            "audio": audio,
            "batch_size": batch_size,
        }


class _WhisperXStub:
    """Minimal whisperx stand-in. Injected via sys.modules — CI has no model."""

    __version__ = "0.0-test"

    def __init__(self) -> None:
        self.load_calls: list[dict] = []
        self.align_calls = 0
        self.diarize_calls = 0

    def load_audio(self, path):
        return ["waveform", str(path)]

    def load_model(self, name, device, compute_type="float16", language=None, asr_options=None, **kwargs):
        self.load_calls.append(
            {
                "name": name,
                "device": device,
                "compute_type": compute_type,
                "language": language,
                "asr_options": asr_options or {},
                "vad_method": kwargs.get("vad_method"),
                "device_index": kwargs.get("device_index", 0),
            }
        )
        return _StubModel()

    def load_align_model(self, language_code, device, **kwargs):
        return ("align-model", {"language": language_code, "device": device})

    def align(self, segments, model_a, metadata, audio, device, return_char_alignments=False):
        self.align_calls += 1
        out = []
        for seg in segments:
            words = []
            start = float(seg["start"])
            for i, token in enumerate(str(seg["text"]).split()):
                words.append(
                    {
                        "word": token,
                        "start": start + i * 0.25,
                        "end": start + (i + 1) * 0.25,
                        "score": 0.99,
                    }
                )
            item = dict(seg)
            item["words"] = words
            out.append(item)
        return {"segments": out, "language": metadata.get("language", "de")}

    def DiarizationPipeline(self, *args, **kwargs):
        self.diarize_calls += 1
        raise AssertionError("diarization must not run")


def _install_stub(monkeypatch: pytest.MonkeyPatch) -> _WhisperXStub:
    stub = _WhisperXStub()
    monkeypatch.setitem(sys.modules, "whisperx", stub)
    return stub


def test_default_asr_provider_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TVA_ASR_PROVIDER", raising=False)
    assert get_asr_provider().name == "fake"
    assert get_asr_provider("whisperx").name == "whisperx"
    monkeypatch.setenv("TVA_ASR_PROVIDER", "  ")
    assert get_asr_provider().name == "fake"


def test_whisperx_maps_aligned_result() -> None:
    result = {
        "language": "de",
        "segments": [
            {
                "start": 0.0,
                "end": 1.5,
                "text": "Hallo ONH",
                "language": "de",
                "words": [
                    {"word": "Hallo", "start": 0.0, "end": 0.4, "score": 0.9},
                    {"word": "ONH", "start": 0.4, "end": 0.8, "score": 0.8},
                ],
            }
        ],
    }
    transcript = transcript_from_whisperx(
        result, language="de", model="large-v3", prompt_version="jargon-v1+test"
    )
    assert transcript.segments[0].id == "seg_001"
    assert transcript.segments[0].lang == "de"
    assert transcript.segments[0].words[1].w == "ONH"
    assert transcript.language == "de"


def test_whisperx_maps_batched_list_text_and_nan_times() -> None:
    result = {
        "language": "de",
        "segments": [
            {
                "start": float("nan"),
                "end": float("nan"),
                "text": ["Hallo ONH"],
                "words": [
                    {"word": "Hallo", "start": 0.0, "end": 0.4, "score": 0.9},
                    {"word": "ONH", "start": 0.4, "end": 0.8, "score": float("nan")},
                ],
            },
            {
                "start": 1.5,
                "end": 2.0,
                "text": "",
                "words": [{"word": "Playbook", "start": 1.5, "end": 2.0, "score": 0.7}],
            },
        ],
    }
    transcript = transcript_from_whisperx(
        result, language="de", model="large-v3", prompt_version="jargon-v1+test"
    )
    assert transcript.segments[0].text == "Hallo ONH"
    assert transcript.segments[0].t0 == 0.0
    assert transcript.segments[0].t1 == 0.8
    assert transcript.segments[0].words[1].p == 1.0
    assert transcript.segments[1].id == "seg_002"
    assert transcript.segments[1].text == "Playbook"


def test_whisperx_stub_transcribe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = _install_stub(monkeypatch)
    monkeypatch.setenv("TVA_ASR_DEVICE", "cpu")
    monkeypatch.setenv("TVA_ASR_BATCH_SIZE", "4")
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"not-a-real-opus")

    with pytest.warns(UserWarning, match="CPU"):
        transcript = WhisperXAsrProvider().transcribe(audio, language="de")

    assert stub.align_calls == 1
    assert stub.diarize_calls == 0
    assert stub.load_calls[0]["language"] == "de"
    assert stub.load_calls[0]["compute_type"] == "int8"
    assert stub.load_calls[0]["vad_method"] == "silero"
    prompt = stub.load_calls[0]["asr_options"]["initial_prompt"]
    assert "ONH" in prompt
    assert "dVWAP" in prompt
    assert prompt == load_glossary().initial_prompt
    assert transcript.provider == "whisperx"
    assert transcript.model == DEFAULT_WHISPERX_MODEL
    assert "whisperx-" in transcript.prompt_version
    assert transcript.segments[0].id == "seg_001"
    assert transcript.segments[1].id == "seg_002"
    assert transcript.segments[0].lang == "de"
    assert transcript.segments[1].lang == "en"
    assert any(w.w.startswith("ONH") for w in transcript.segments[0].words)


def test_whisperx_cuda_compute_type(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = _install_stub(monkeypatch)
    monkeypatch.setenv("TVA_ASR_DEVICE", "cuda")
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"x")
    WhisperXAsrProvider().transcribe(audio, language="de")
    assert stub.load_calls[0]["compute_type"] == "float16"
    assert stub.load_calls[0]["device"] == "cuda"
    assert stub.load_calls[0]["device_index"] == 0


def test_whisperx_cuda_index_is_not_treated_as_cpu(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    stub = _install_stub(monkeypatch)
    monkeypatch.setenv("TVA_ASR_DEVICE", "cuda:1")
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"x")
    WhisperXAsrProvider().transcribe(audio, language="de")
    assert stub.load_calls[0]["device"] == "cuda"
    assert stub.load_calls[0]["device_index"] == 1
    assert stub.load_calls[0]["compute_type"] == "float16"
    assert resolve_device() == ("cuda", 1)
    assert resolve_compute_type("cuda") == "float16"
    assert resolve_batch_size("cuda") == 16


def test_invalid_batch_size_is_asr_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TVA_ASR_BATCH_SIZE", "nope")
    with pytest.raises(AsrError, match="TVA_ASR_BATCH_SIZE"):
        resolve_batch_size("cpu")
    monkeypatch.setenv("TVA_ASR_BATCH_SIZE", "0")
    with pytest.raises(AsrError, match="TVA_ASR_BATCH_SIZE"):
        resolve_batch_size("cpu")
    monkeypatch.setenv("TVA_ASR_DEVICE", "cuda:x")
    with pytest.raises(AsrError, match="TVA_ASR_DEVICE"):
        resolve_device()


def test_whisperx_rerun_within_20ms(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install_stub(monkeypatch)
    monkeypatch.setenv("TVA_ASR_DEVICE", "cpu")
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"x")
    with pytest.warns(UserWarning, match="CPU"):
        first = WhisperXAsrProvider().transcribe(audio, language="de")
    with pytest.warns(UserWarning, match="CPU"):
        second = WhisperXAsrProvider().transcribe(audio, language="de")
    assert transcripts_close(first, second, word_tol_s=WORD_TIME_TOLERANCE_S)

    shifted = Transcript(
        provider=first.provider,
        model=first.model,
        prompt_version=first.prompt_version,
        language=first.language,
        segments=[
            TranscriptSegment(
                id=seg.id,
                t0=seg.t0 + 0.010,
                t1=seg.t1 + 0.010,
                lang=seg.lang,
                text=seg.text,
                words=[
                    TranscriptWord(w=w.w, t0=w.t0 + 0.010, t1=w.t1 + 0.010, p=w.p) for w in seg.words
                ],
            )
            for seg in first.segments
        ],
    )
    assert transcripts_close(first, shifted, word_tol_s=WORD_TIME_TOLERANCE_S)
    too_far = Transcript(
        provider=first.provider,
        model=first.model,
        prompt_version=first.prompt_version,
        language=first.language,
        segments=[
            TranscriptSegment(
                id=seg.id,
                t0=seg.t0 + 0.050,
                t1=seg.t1 + 0.050,
                lang=seg.lang,
                text=seg.text,
                words=seg.words,
            )
            for seg in first.segments
        ],
    )
    assert not transcripts_close(first, too_far, word_tol_s=WORD_TIME_TOLERANCE_S)


def test_whisperx_missing_extra(tmp_path: Path) -> None:
    if whisperx_importable():
        pytest.skip("whisperx is installed in this environment")
    audio = tmp_path / "mic.opus"
    audio.write_bytes(b"x")
    with pytest.raises(AsrError, match="not installed"):
        WhisperXAsrProvider().transcribe(audio)


def test_doctor_reports_whisperx_and_cuda(tva_root: Path) -> None:
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert "whisperx" in ids
    assert "cuda" in ids
    assert ids["whisperx"].status in {"ok", "warn"}
    assert ids["cuda"].status in {"ok", "warn"}
    assert report.ok


def test_doctor_normalizes_provider_and_fails_if_selected(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if whisperx_importable():
        pytest.skip("whisperx is installed in this environment")
    monkeypatch.setenv("TVA_ASR_PROVIDER", " WhisperX ")
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["asr_provider"].status == "ok"
    assert ids["asr_provider"].detail == "TVA_ASR_PROVIDER=whisperx"
    assert ids["whisperx"].status == "fail"
    assert not report.ok


@pytest.mark.golden
def test_golden_whisperx_wer_and_jargon(tmp_path: Path) -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    ref = golden / "reference.txt"
    if not ref.is_file():
        pytest.skip("golden reference.txt absent")
    media = sorted(golden.glob("*.mp4")) + sorted(golden.glob("*.mkv"))
    if not media:
        pytest.skip("golden media absent")
    try:
        import whisperx  # noqa: F401
    except ImportError:
        pytest.skip("whisperx not installed")

    root = tmp_path / "store"
    config.ensure_layout(root)
    record = ingest(media[0], root=root)
    transcribe_session(record.id, root=root, provider_name="whisperx")
    report = score_session(record.id, ref=ref, root=root)
    assert report["wer"] <= 0.10
    assert report["jargon_recall"] >= 0.90
