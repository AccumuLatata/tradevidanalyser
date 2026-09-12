"""The Grok routine pack must match the PR-10 HTTP API."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from tradevidanalyser.schema import EVENT_KINDS, Insights, SessionStatus
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, _within_days

ROOT = Path(__file__).resolve().parents[1]
PACK = (ROOT / "docs" / "TVA_GROK_ROUTINE_PACK.md").read_text(encoding="utf-8")
SYSTEM = (ROOT / "examples" / "bot" / "SYSTEM.md").read_text(encoding="utf-8")
EVENING = (ROOT / "examples" / "bot" / "ROUTINE_EVENING.md").read_text(encoding="utf-8")
SUNDAY = (ROOT / "examples" / "bot" / "ROUTINE_SUNDAY_AUDIT.md").read_text(encoding="utf-8")
ALL = "\n".join((PACK, SYSTEM, EVENING, SUNDAY))


def test_pack_lists_real_endpoints() -> None:
    for path in (
        "/health",
        "/sessions?days=1",
        "/sessions?days=7",
        "/sessions/latest",
        "/sessions/{id}/status",
        "/sessions/{id}/transcript",
        "/sessions/{id}/insights",
        "/sessions/{id}/run?stages=transcribe,extract",
    ):
        assert path in ALL, path
    assert "GET /sessions/{id}/clips" in PACK
    assert "Do not call" in PACK or "Do not" in PACK


def test_system_allow_list_includes_today_and_week() -> None:
    assert "GET $TVA_API_BASE/sessions?days=1" in SYSTEM
    assert "GET $TVA_API_BASE/sessions?days=7" in SYSTEM
    assert "GET $TVA_API_BASE/sessions/latest" in SYSTEM
    assert "POST $TVA_API_BASE/sessions/{id}/run?stages=transcribe,extract" in SYSTEM


def test_days_windows_match_serve() -> None:
    today = date(2026, 9, 12)
    assert _within_days("2026-09-12_143000", 1, today=today)
    assert not _within_days("2026-09-11_143000", 1, today=today)
    assert _within_days("2026-09-06_100000", 7, today=today)
    assert not _within_days("2026-09-05_100000", 7, today=today)
    assert "today only" in SYSTEM
    assert "age < 1" in PACK
    assert "0 ≤ age < 7" in PACK


def test_run_contract_202_and_409() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "202" in EVENING and "202" in PACK
    assert "409" in EVENING and "409" in SYSTEM
    assert "do not POST again" in EVENING or "do not POST again" in SYSTEM


def test_pack_does_not_index_segments_as_a_dict() -> None:
    assert "segments[seg]" not in ALL
    assert "segment.id" in ALL or "whose `id`" in ALL
    assert "array" in SYSTEM.lower() or "**array**" in EVENING


def test_today_vienna_guard_and_watchdog_key() -> None:
    assert "Europe/Vienna" in EVENING
    assert "starts with" in EVENING
    assert "yesterday" in EVENING.lower() or "previous day" in EVENING
    assert "session id" in EVENING
    assert "status ok" in EVENING
    assert "does not count" in EVENING or "do not count" in PACK.lower()


def test_health_root_is_tva_root_and_must_not_be_copied() -> None:
    assert "TVA_ROOT" in SYSTEM
    assert "`root`" in SYSTEM or "→ `root`" in SYSTEM
    assert "Never copy" in PACK or "never copy" in SYSTEM.lower()


def test_status_words_and_event_kinds_match_schema() -> None:
    stages = SessionStatus.model_fields["stages"]
    assert stages is not None
    for word in ("ok", "missing", "failed", "running"):
        assert word in SYSTEM
        assert word in EVENING
    for kind in EVENT_KINDS:
        assert kind in PACK
    fields = set(Insights.model_fields)
    for name in (
        "bias_statements",
        "playbooks_mentioned",
        "stated_levels",
        "stated_stops_targets",
        "checkins",
        "tilt_markers",
        "brief_refs",
        "observations",
        "gaps",
        "session_events",
        "summary_de",
        "summary_en",
    ):
        assert name in fields
        assert name in PACK


def test_summary_digit_run_rule_matches_extractor() -> None:
    assert "digit run" in PACK
    assert "120" in PACK
    assert "exact" in PACK


def test_sunday_empty_week_pings_and_counts_running() -> None:
    assert "no sessions in 7 days" in SUNDAY
    assert "M + F + R" in SUNDAY
    assert "running" in SUNDAY
    assert "oldest-first" in SUNDAY or "oldest first" in SUNDAY


def test_auth_and_bundle_404_semantics() -> None:
    assert "Authorization: Bearer $TVA_API_TOKEN" in SYSTEM
    assert "401" in SYSTEM
    assert "404" in EVENING
    assert "null" in PACK
