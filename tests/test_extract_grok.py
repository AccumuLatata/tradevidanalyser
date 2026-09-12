from __future__ import annotations

import json
import os
import unicodedata
from pathlib import Path

import httpx
import pytest

from tradevidanalyser import config
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, transcribe_session
from tradevidanalyser.providers.extract import (
    DEFAULT_GROK_MODEL,
    EVENTS_PROMPT_FILENAME,
    HTTP_TIMEOUT,
    XAI_CHAT_URL,
    ExtractError,
    FakeExtractProvider,
    GrokExtractProvider,
    apply_citation_guard,
    default_prompt_path,
    get_extract_provider,
    insights_from_chat_payload,
    insights_json_schema,
    merge_partial_insights,
    prompt_version_for,
    window_segments,
)
from tradevidanalyser.schema import CitedSpan, Insights, Transcript, TranscriptSegment

FIXTURES = Path(__file__).parent / "fixtures"
VALID = FIXTURES / "grok_insights_valid.json"
FABRICATED = FIXTURES / "grok_insights_fabricated.json"
UNKNOWN = FIXTURES / "grok_insights_unknown_seg.json"

PLANTED_TEXTS = (
    "Bias ist long.",
    "Playbook ONH Touch.",
    "Stop unter dem Level. Ziel am ONH.",
    "Check-in zur vollen Stunde.",
)


def _payload(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _chat_response(insights: dict) -> dict:
    return {"choices": [{"message": {"content": json.dumps(insights, ensure_ascii=False)}}]}


def _empty_events_body() -> dict:
    return {"session_events": [], "summary_de": "", "summary_en": ""}


def _is_events_request(request: httpx.Request) -> bool:
    body = json.loads(request.content)
    return body.get("response_format", {}).get("json_schema", {}).get("name") == "session_events"


def _with_empty_events(handler):
    def wrapped(request: httpx.Request) -> httpx.Response:
        if _is_events_request(request):
            return httpx.Response(200, json=_chat_response(_empty_events_body()))
        return handler(request)

    return wrapped


def _transcript(texts: tuple[str, ...] = PLANTED_TEXTS) -> Transcript:
    segments = [
        TranscriptSegment(
            id=f"seg_{index:03d}",
            t0=float(index - 1),
            t1=float(index),
            lang="de",
            text=text,
        )
        for index, text in enumerate(texts, start=1)
    ]
    return Transcript(provider="fake", model="fake-v1", language="de", segments=segments)


def _provider(handler, monkeypatch: pytest.MonkeyPatch, **kwargs) -> GrokExtractProvider:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    return GrokExtractProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_get_extract_provider_default_is_fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TVA_EXTRACT_PROVIDER", raising=False)
    assert get_extract_provider().name == "fake"
    assert get_extract_provider("grok").name == "grok"
    assert get_extract_provider("xai").name == "grok"


def test_window_segments_overlap() -> None:
    items = list(range(45))
    windows = window_segments(items, size=40, overlap=5)
    assert windows[0] == items[:40]
    assert windows[1][0] == items[35]
    assert windows[-1][-1] == items[-1]
    assert all(len(window) <= 40 for window in windows)


def test_merge_is_order_independent() -> None:
    left = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_002", text="b")],
        playbooks_mentioned=[CitedSpan(seg="seg_001", text="play-a")],
    )
    right = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_001", text="a")],
        playbooks_mentioned=[CitedSpan(seg="seg_001", text="play-b")],
    )
    kwargs = {"provider": "grok", "model": "m", "prompt_version": "v"}
    ab = merge_partial_insights([left, right], **kwargs)
    ba = merge_partial_insights([right, left], **kwargs)
    assert [s.seg for s in ab.bias_statements] == [s.seg for s in ba.bias_statements]
    assert [s.text for s in ab.bias_statements] == [s.text for s in ba.bias_statements]
    assert len(ab.playbooks_mentioned) == 1
    assert ab.playbooks_mentioned[0].text == ba.playbooks_mentioned[0].text
    assert ab.playbooks_mentioned[0].text == "play-a"


def test_merge_prefers_longer_quote() -> None:
    short = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_001", text="Bias")],
    )
    long = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_001", text="Bias ist long")],
    )
    kwargs = {"provider": "grok", "model": "m", "prompt_version": "v"}
    assert merge_partial_insights([short, long], **kwargs).bias_statements[0].text == "Bias ist long"
    assert merge_partial_insights([long, short], **kwargs).bias_statements[0].text == "Bias ist long"


def test_default_prompt_path_finds_repo_file_when_cwd_elsewhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    path = default_prompt_path()
    assert path.is_file()
    assert path.name == "insights_v1.de.md"


def test_grok_valid_mock_keeps_citations(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=_chat_response(_payload(VALID)))

    insights = _provider(_with_empty_events(handler), monkeypatch).extract(_transcript())
    window_reqs = [item for item in captured if not _is_events_request(item)]
    assert len(window_reqs) == 1
    request = window_reqs[0]
    assert str(request.url) == XAI_CHAT_URL
    assert request.headers["authorization"] == "Bearer xai-test-key"
    body = json.loads(request.content)
    assert body["model"] == DEFAULT_GROK_MODEL
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] == insights_json_schema()
    assert "seg_001" in body["messages"][1]["content"]
    timeout = request.extensions.get("timeout")
    if isinstance(timeout, dict):
        assert timeout.get("read") == HTTP_TIMEOUT
    elif timeout is not None:
        assert getattr(timeout, "read", None) == HTTP_TIMEOUT
    assert insights.bias_statements[0].seg == "seg_001"
    assert insights.playbooks_mentioned[0].seg == "seg_002"
    assert insights.checkins[0].text == "Check-in zur vollen Stunde"
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    assert insights.prompt_version == (
        f"{prompt_version_for(prompts / 'insights_v1.de.md')}"
        f"+{prompt_version_for(prompts / EVENTS_PROMPT_FILENAME)}"
    )
    assert not any(item.startswith("dropped ") for item in insights.gaps)


def test_grok_fabricated_quote_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(_payload(FABRICATED)))

    insights = _provider(_with_empty_events(handler), monkeypatch).extract(_transcript())
    assert insights.bias_statements == []
    assert any("quote not found" in gap for gap in insights.gaps)
    assert any("seg_001" in gap for gap in insights.gaps)


def test_grok_unknown_seg_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_chat_response(_payload(UNKNOWN)))

    insights = _provider(_with_empty_events(handler), monkeypatch).extract(_transcript())
    assert insights.playbooks_mentioned == []
    assert any("seg_999" in gap for gap in insights.gaps)
    assert any("not in the transcript" in gap for gap in insights.gaps)


def test_grok_three_windows_valid_fabricated_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    fabricated = _payload(FABRICATED)
    fabricated["bias_statements"] = []
    fabricated["observations"] = [{"seg": "seg_002", "text": "komplett erfundener Satz"}]
    replies = [
        _chat_response(_payload(VALID)),
        _chat_response(fabricated),
        _chat_response(_payload(UNKNOWN)),
    ]
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=replies.pop(0))

    insights = _provider(_with_empty_events(handler), monkeypatch, window_size=2, overlap=1).extract(
        _transcript()
    )
    assert len([item for item in captured if not _is_events_request(item)]) == 3
    assert insights.bias_statements[0].text == "Bias ist long"
    assert insights.playbooks_mentioned[0].seg == "seg_002"
    assert insights.observations == []
    assert any("quote not found" in gap for gap in insights.gaps)
    assert any("seg_999" in gap for gap in insights.gaps)


def test_grok_out_of_window_seg_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = Insights(provider="grok", model=DEFAULT_GROK_MODEL).model_dump(mode="json")
    replies = [_chat_response(_payload(VALID)), _chat_response(empty)]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=replies.pop(0))

    # Two segs, one per window. VALID cites seg_002, which is on the tape but
    # not in the first window — that citation must not be laundered.
    two = _transcript(PLANTED_TEXTS[:2])
    insights = _provider(_with_empty_events(handler), monkeypatch, window_size=1, overlap=0).extract(
        two
    )
    assert insights.bias_statements[0].seg == "seg_001"
    assert insights.playbooks_mentioned == []
    assert any("seg_002" in gap for gap in insights.gaps)


def test_extract_http_never_called_when_provider_is_fake(
    monkeypatch: pytest.MonkeyPatch, tva_root: Path, sample_video: Path
) -> None:
    def fail_send(self, request, *args, **kwargs):
        raise AssertionError(f"extract HTTP must not run for fake: {request.url}")

    monkeypatch.setattr(httpx.Client, "send", fail_send)
    monkeypatch.delenv("TVA_EXTRACT_PROVIDER", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root, provider_name="fake")
    insights = extract_session(record.id, root=tva_root, provider_name="fake")
    assert insights.provider == "fake"
    assert isinstance(get_extract_provider(), FakeExtractProvider)


def test_grok_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    with pytest.raises(ExtractError, match="XAI_API_KEY"):
        GrokExtractProvider().extract(_transcript())


def test_insights_from_chat_payload_roundtrip() -> None:
    parsed = insights_from_chat_payload(_chat_response(_payload(VALID)))
    assert parsed.bias_statements[0].seg == "seg_001"


def test_insights_from_chat_payload_accepts_object_content() -> None:
    body = {"bias_statements": [{"seg": "seg_001", "text": "Bias ist long"}]}
    parsed = insights_from_chat_payload({"choices": [{"message": {"content": body}}]})
    assert parsed.provider == "grok"
    assert parsed.bias_statements[0].seg == "seg_001"


def test_insights_from_chat_payload_reports_refusal() -> None:
    with pytest.raises(ExtractError, match="refused"):
        insights_from_chat_payload(
            {"choices": [{"message": {"content": "", "refusal": "filtered"}}]}
        )


def test_apply_citation_guard_keeps_valid() -> None:
    transcript = _transcript()
    raw = Insights.model_validate(_payload(VALID))
    cleaned = apply_citation_guard(transcript, raw)
    assert cleaned.bias_statements
    assert cleaned.checkins


def test_apply_citation_guard_drops_fabricated_raw_text() -> None:
    transcript = _transcript(("Bias ist long.",))
    raw = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_001", text="Bias ist long")],
        stated_stops_targets=[CitedSpan(seg="seg_001", text="", raw_text="Stop bei 18500")],
    )
    cleaned = apply_citation_guard(transcript, raw)
    assert cleaned.bias_statements
    assert cleaned.stated_stops_targets == []
    assert any("raw_text not found" in gap for gap in cleaned.gaps)


def test_apply_citation_guard_accepts_nfc_equivalent_quote() -> None:
    nfc = unicodedata.normalize("NFC", "Prüfung bestanden")
    nfd = unicodedata.normalize("NFD", "Prüfung")
    transcript = Transcript(
        provider="fake",
        model="fake-v1",
        language="de",
        segments=[
            TranscriptSegment(id="seg_001", t0=0.0, t1=1.0, lang="de", text=nfc),
        ],
    )
    raw = Insights(
        provider="grok",
        model="m",
        bias_statements=[CitedSpan(seg="seg_001", text=nfd)],
    )
    cleaned = apply_citation_guard(transcript, raw)
    assert cleaned.bias_statements
    assert not any(item.startswith("dropped ") for item in cleaned.gaps)


def test_doctor_grok_without_key_fails(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TVA_EXTRACT_PROVIDER", "grok")
    monkeypatch.setenv("XAI_API_KEY", "   ")
    report = run_doctor(tva_root)
    ids = {c.id: c for c in report.checks}
    assert ids["xai_key"].status == "warn"
    assert ids["extract_provider"].status == "fail"
    assert not report.ok


@pytest.mark.golden
def test_golden_excerpt_planted_insights() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    planted_path = golden / "planted.yaml"
    if not planted_path.is_file():
        pytest.skip("golden planted.yaml absent")
    transcript_path = golden / "transcript.json"
    if not transcript_path.is_file():
        pytest.skip("golden transcript.json absent")
    planted = _load_planted(planted_path)
    transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    key = os.environ.get("XAI_API_KEY", "").strip()
    insights_path = golden / "insights.json"
    if key:
        insights = GrokExtractProvider().extract(transcript)
    elif insights_path.is_file():
        insights = Insights.model_validate_json(insights_path.read_text(encoding="utf-8"))
    else:
        pytest.skip("XAI_API_KEY unset and golden/insights.json absent")

    known = {seg.id: seg.text for seg in transcript.segments}
    _assert_planted(insights.bias_statements, planted.get("bias"), known)
    _assert_planted(insights.playbooks_mentioned, planted.get("playbook"), known)
    stop_target = " ".join(filter(None, [planted.get("stop_raw"), planted.get("target_raw")]))
    _assert_planted(insights.stated_stops_targets, planted.get("stop_raw") or stop_target, known)
    if planted.get("target_raw"):
        _assert_planted(insights.stated_stops_targets, planted["target_raw"], known)
    _assert_planted(insights.checkins, planted.get("checkin") or planted.get("checkin_t"), known)


def _load_planted(path: Path) -> dict[str, str]:
    planted: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        planted[key.strip()] = value.strip().strip("\"'")
    return planted


def _assert_planted(spans: list[CitedSpan], needle: str | None, known: dict[str, str]) -> None:
    if not needle:
        pytest.skip("planted field missing")
    blob = needle.strip()
    if not blob:
        pytest.skip("planted field empty")
    matched = False
    for span in spans:
        assert span.seg in known
        if span.text:
            assert span.text in known[span.seg]
        hay = " ".join(filter(None, [span.text, span.name, span.token, span.raw_text]))
        if blob in hay or blob in known[span.seg]:
            matched = True
    assert matched, f"planted {blob!r} not cited"
