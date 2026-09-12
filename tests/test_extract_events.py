from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from tradevidanalyser import config
from tradevidanalyser.providers.extract import (
    GrokExtractProvider,
    SessionEventDraft,
    apply_citation_guard,
    events_from_chat_payload,
    hydrate_session_events,
)
from tradevidanalyser.schema import (
    EVENT_KINDS,
    SCHEMA_VERSION,
    Insights,
    SessionEvent,
    Transcript,
    TranscriptSegment,
)

VALID = Path(__file__).parent / "fixtures" / "grok_insights_valid.json"
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


def _is_events_request(request: httpx.Request) -> bool:
    body = json.loads(request.content)
    return body.get("response_format", {}).get("json_schema", {}).get("name") == "session_events"


def _provider(handler, monkeypatch: pytest.MonkeyPatch, **kwargs) -> GrokExtractProvider:
    monkeypatch.setenv("XAI_API_KEY", "xai-test-key")
    return GrokExtractProvider(
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        **kwargs,
    )


def test_session_event_kind_closed_set() -> None:
    for kind in EVENT_KINDS:
        event = SessionEvent(t=0.0, kind=kind, seg="seg_001", text="x")
        assert event.kind == kind
    with pytest.raises(ValidationError):
        SessionEvent(t=0.0, kind="not_a_kind", seg="seg_001", text="x")  # type: ignore[arg-type]


def test_insights_additive_no_schema_bump() -> None:
    payload = {
        "provider": "fake",
        "model": "keyword-v1",
        "bias_statements": [{"seg": "seg_001", "text": "Bias ist long"}],
    }
    insights = Insights.model_validate(payload)
    assert insights.schema_version == SCHEMA_VERSION == "1"
    assert insights.session_events == []
    assert insights.summary_de == ""
    assert insights.summary_en == ""


def test_summary_digit_leak_dropped() -> None:
    transcript = _transcript(("Bias ist long.",))
    raw = Insights(
        provider="grok",
        model="m",
        bias_statements=[{"seg": "seg_001", "text": "Bias ist long"}],
        summary_de="Stop bei 18500",
        summary_en="Stopped at 18500",
    )
    cleaned = apply_citation_guard(transcript, raw)
    assert cleaned.summary_de == ""
    assert cleaned.summary_en == ""
    assert any("18500" in gap for gap in cleaned.gaps)


def test_summary_digit_from_cited_segment_kept() -> None:
    transcript = _transcript(("Stop bei 18500.",))
    raw = Insights(
        provider="grok",
        model="m",
        stated_stops_targets=[
            {"seg": "seg_001", "text": "Stop bei 18500", "raw_text": "Stop bei 18500"}
        ],
        summary_de="Im Tape: 18500 genannt.",
    )
    cleaned = apply_citation_guard(transcript, raw)
    assert "18500" in cleaned.summary_de
    assert not any("digit" in gap for gap in cleaned.gaps)


def test_summary_de_over_120_words_dropped() -> None:
    transcript = _transcript(("Bias ist long.",))
    summary = " ".join(["Wort"] * 121)
    raw = Insights(
        provider="grok",
        model="m",
        bias_statements=[{"seg": "seg_001", "text": "Bias ist long"}],
        summary_de=summary,
    )
    cleaned = apply_citation_guard(transcript, raw)
    assert cleaned.summary_de == ""
    assert any("120 words" in gap for gap in cleaned.gaps)


def test_hydrate_events_uses_transcript_text() -> None:
    transcript = _transcript()
    events, gaps = hydrate_session_events(
        [SessionEventDraft(kind="bias_statement", seg="seg_001")],
        transcript,
    )
    assert events[0].text == transcript.segments[0].text
    assert events[0].t == transcript.segments[0].t0
    assert not gaps


def test_hydrate_drops_unknown_kind_and_seg() -> None:
    transcript = _transcript()
    events, gaps = hydrate_session_events(
        [
            SessionEventDraft(kind="made_up", seg="seg_001"),
            SessionEventDraft(kind="tilt", seg="seg_999"),
        ],
        transcript,
    )
    assert events == []
    assert any("unknown kind" in gap for gap in gaps)
    assert any("seg_999" in gap for gap in gaps)


def test_events_pass_sends_ids_not_text_and_hydrates(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[httpx.Request] = []
    events_body = {
        "session_events": [{"kind": "bias_statement", "seg": "seg_001"}],
        "summary_de": "Bias long.",
        "summary_en": "Bias long.",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if _is_events_request(request):
            return httpx.Response(200, json=_chat_response(events_body))
        return httpx.Response(200, json=_chat_response(_payload(VALID)))

    insights = _provider(handler, monkeypatch).extract(_transcript())
    events_reqs = [item for item in captured if _is_events_request(item)]
    assert len(events_reqs) == 1
    body = json.loads(events_reqs[0].content)
    user = body["messages"][1]["content"]
    assert "seg_001" in user
    assert "Bias ist long" not in user
    assert body["response_format"]["json_schema"]["name"] == "session_events"
    assert insights.session_events
    assert insights.session_events[0].kind == "bias_statement"
    assert insights.session_events[0].text == "Bias ist long."
    assert insights.summary_de == "Bias long."


def test_events_from_chat_payload_roundtrip() -> None:
    parsed = events_from_chat_payload(
        _chat_response(
            {
                "session_events": [{"kind": "hourly_checkin", "seg": "seg_004"}],
                "summary_de": "Check-in.",
            }
        )
    )
    assert parsed.session_events[0].seg == "seg_004"
    assert parsed.summary_de == "Check-in."


@pytest.mark.golden
def test_golden_excerpt_session_events_and_summary() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    transcript_path = golden / "transcript.json"
    if not transcript_path.is_file():
        pytest.skip("golden transcript.json absent")
    transcript = Transcript.model_validate_json(transcript_path.read_text(encoding="utf-8"))
    key = os.environ.get("XAI_API_KEY", "").strip()
    insights_path = golden / "insights.json"
    if key:
        insights = GrokExtractProvider().extract(transcript)
    elif insights_path.is_file():
        insights = Insights.model_validate_json(insights_path.read_text(encoding="utf-8"))
        if not insights.session_events:
            pytest.skip("golden insights.json has no session_events")
    else:
        pytest.skip("XAI_API_KEY unset and golden/insights.json absent")

    assert insights.session_events, "insights.session_events must be non-empty"
    assert len((insights.summary_de or "").split()) <= 120
    cleaned = apply_citation_guard(transcript, insights)
    assert not any("digit" in gap for gap in cleaned.gaps)
    assert not any("120 words" in gap for gap in cleaned.gaps)
    for event in insights.session_events:
        assert event.kind in EVENT_KINDS
        assert event.seg in {seg.id for seg in transcript.segments}
