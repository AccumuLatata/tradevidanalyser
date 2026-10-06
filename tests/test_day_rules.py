from __future__ import annotations

import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.coach import gather_pack
from tradevidanalyser.day_manifest import DayStale, build_day, load_current_day_json
from tradevidanalyser.flags import (
    ENV_DAY_MANIFEST,
    ENV_DAY_PUBLISH,
    ENV_DAY_RULES,
    ENV_EXCLUSIVE_FILLS,
    ENV_PAUSE_GUARD,
    ENV_TRADING_HOURS,
)
from tradevidanalyser.ledger import (
    DAY_RULE_IDS,
    drop_day_rows,
    ledger_add,
    ledger_db_path,
    rollup,
    window_session_ids,
)
from tradevidanalyser.pause_guard import PauseCheck, write_pause_check
from tradevidanalyser.pipeline import fills_session, rules_session
from tradevidanalyser.rules import (
    EVIDENCE_BACKED_RULE_IDS,
    EVIDENCE_PENDING_REASON,
    RULE_IDS,
    day_rule_reason,
)
from tradevidanalyser.schema import (
    Alignment,
    Evidence,
    Insights,
    IntentProposal,
    IntentProposals,
    RecordingInfo,
    SessionEvent,
    SessionRecord,
)
from l0_parity import json_files_equal

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_SESSION = "2026-05-14_160300"
VIENNA = timezone(timedelta(hours=2))
DAY = date(2026, 9, 14)
CSV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)


def _four_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        ENV_PAUSE_GUARD,
        ENV_DAY_MANIFEST,
        ENV_EXCLUSIVE_FILLS,
        ENV_DAY_RULES,
        ENV_DAY_PUBLISH,
        ENV_TRADING_HOURS,
    ):
        monkeypatch.setenv(name, "1")


def _manifest_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_EXCLUSIVE_FILLS, raising=False)
    monkeypatch.delenv(ENV_DAY_RULES, raising=False)
    monkeypatch.delenv(ENV_DAY_PUBLISH, raising=False)
    monkeypatch.delenv(ENV_TRADING_HOURS, raising=False)
    monkeypatch.setenv(ENV_PAUSE_GUARD, "1")
    monkeypatch.setenv(ENV_DAY_MANIFEST, "1")


def _session(
    root: Path,
    session_id: str,
    *,
    start: datetime,
    duration_s: float,
    sha256: str | None = None,
) -> SessionRecord:
    name = f"{session_id[:10]} {session_id[11:13]}-{session_id[13:15]}-{session_id[15:17]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{name}",
            sha256=sha256 or ("a" * 64),
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename=name,
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    write_pause_check(
        root,
        PauseCheck(
            session_id=record.id,
            recording_sha256=record.recording.sha256,
            part_shas=[],
            duration_s=float(record.recording.duration_s),
            pause_check="clear",
            pause_total_s=0.0,
            clock_resolution_s=1,
            pause_detectable_from_s=30.0,
            ocr_provider="injected",
            ocr_model="test",
        ),
    )
    return record


def _csv_row(
    ts: str,
    *,
    side: str = "buy",
    price: str = "21000.0",
    spread: str = "g1",
) -> str:
    return (
        f"{ts},MNQM26,{side},USD,MNQ,future,{price},1.0,"
        f"0,0,N/A,N/A,,,{spread}"
    )


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _win(entry: str, exit_ts: str, *, spread: str, price: str = "21000.0") -> list[str]:
    return [
        _csv_row(entry, side="buy", price=price, spread=spread),
        _csv_row(exit_ts, side="sell", price=str(float(price) + 1), spread=spread),
    ]


def _loss(entry: str, exit_ts: str, *, spread: str, price: str = "21000.0") -> list[str]:
    return [
        _csv_row(entry, side="buy", price=price, spread=spread),
        _csv_row(exit_ts, side="sell", price=str(float(price) - 10), spread=spread),
    ]


def _two_clips(root: Path) -> tuple[SessionRecord, SessionRecord]:
    a = _session(
        root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    b = _session(
        root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    return a, b


def _rules(root: Path, session_id: str) -> dict[str, dict]:
    payload = store.read_json(store.rules_path(root, session_id))
    return {item["rule"]: item for item in payload["rules"]}


def _day_rules(root: Path, day: date = DAY) -> dict[str, dict]:
    payload = load_current_day_json(root, day)
    assert payload is not None
    pointer = (root / "days" / day.isoformat() / "current").read_text(encoding="utf-8").strip()
    body = store.read_json(root / "days" / day.isoformat() / "builds" / pointer / "rules.json")
    return {item["rule"]: item for item in body["rules"]}


def _ledger_day(root: Path, day: date = DAY) -> list[tuple]:
    con = duckdb.connect(str(ledger_db_path(root)), read_only=True)
    try:
        return con.execute(
            "SELECT rule, status, reason FROM day_rule_checks WHERE day = ? ORDER BY 1",
            [day],
        ).fetchall()
    finally:
        con.close()


def test_m7_counts_in_r_max10(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, b = _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T08:30:00+0000", "2026-09-14T08:31:00+0000", spread="gap"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    day = _day_rules(tva_root)
    assert day["R-MAX10"]["evidence"]["count"] == 1
    assert "T01" in day["R-MAX10"]["evidence"]["trade_ids"]
    assert day["R-MAX10"]["status"] == "pass"
    for session_id in (a.id, b.id):
        session = _rules(tva_root, session_id)
        assert session["R-MAX10"]["status"] == "unverifiable"
        assert session["R-MAX10"]["reason"] == day_rule_reason(DAY)
        assert not store.evidence_path(tva_root, session_id).is_file()


def test_m9_r3l30_one_day_violation(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    b = _session(
        tva_root,
        "2026-09-14_102000",
        start=datetime(2026, 9, 14, 10, 20, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    c = _session(
        tva_root,
        "2026-09-14_113200",
        start=datetime(2026, 9, 14, 11, 32, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_loss("2026-09-14T07:01:00+0000", "2026-09-14T07:02:00+0000", spread="a"),
            *_loss("2026-09-14T08:21:00+0000", "2026-09-14T08:22:00+0000", spread="b"),
            *_loss("2026-09-14T09:33:00+0000", "2026-09-14T09:34:00+0000", spread="c1"),
            *_win("2026-09-14T09:36:00+0000", "2026-09-14T09:37:00+0000", spread="c2"),
        ],
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    day = _day_rules(tva_root)
    assert day["R-3L30"]["status"] == "violated"
    rows = {rule: status for rule, status, _reason in _ledger_day(tva_root)}
    assert rows["R-3L30"] == "violated"
    summary = rollup(tva_root).summary
    assert summary.violations.total == 1
    assert summary.violations.by_rule.get("R-3L30") == 1
    pack = gather_pack(tva_root, weeks=4, min_n=1)
    assert not any(item.startswith("day:") for item in pack.session_ids)
    assert not any(
        row.get("rule") == "R-3L30" and row.get("status") == "violated" for row in pack.rules
    )
    for session_id in (a.id, b.id, c.id):
        session = _rules(tva_root, session_id)
        assert session["R-3L30"]["status"] == "unverifiable"
        assert session["R-3L30"]["reason"] == day_rule_reason(DAY)
    assert all(not item.startswith("day:") for item in window_session_ids(tva_root, weeks=4))


def test_m21_build_without_provider_keeps_rule_rows(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    ledger_add(a.id, root=tva_root)
    session = _rules(tva_root, a.id)
    assert set(session) == set(RULE_IDS)
    for rule in DAY_RULE_IDS:
        assert session[rule]["status"] == "unverifiable"
        assert session[rule]["reason"] == day_rule_reason(DAY)
    for rule in EVIDENCE_BACKED_RULE_IDS:
        if rule == "R-SLTP":
            assert session[rule]["reason"] == (
                "order modifications not in TradesViz; needs a venue adapter"
            )
            continue
        if rule == "R-BIAS":
            assert session[rule]["status"] != "violated"
            assert session[rule]["reason"] == EVIDENCE_PENDING_REASON
            continue
        assert session[rule]["status"] == "unverifiable"
        assert session[rule]["reason"] == EVIDENCE_PENDING_REASON
    assert session["R-CLOSE"]["reason"] == "flat_by is unset"
    con = duckdb.connect(str(ledger_db_path(tva_root)), read_only=True)
    try:
        n = con.execute(
            "SELECT count(*) FROM rule_checks WHERE session_id = ?", [a.id]
        ).fetchone()
    finally:
        con.close()
    assert n is not None and n[0] == len(RULE_IDS)


def test_m23_switch_from_manifest_rewrites_rules_keeps_evidence(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _manifest_only(monkeypatch)
    a, b = _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_loss("2026-09-14T07:01:00+0000", "2026-09-14T07:02:00+0000", spread="a1"),
            *_loss("2026-09-14T07:03:00+0000", "2026-09-14T07:04:00+0000", spread="a2"),
            *_loss("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b1"),
            *_win("2026-09-14T09:08:00+0000", "2026-09-14T09:09:00+0000", spread="b2"),
        ],
    )
    fills_session(a.id, root=tva_root, executions=csv, venue="amp")
    fills_session(b.id, root=tva_root, executions=csv, venue="amp")
    store.write_json(
        store.evidence_path(tva_root, a.id),
        Evidence(
            provider="fake",
            model="none",
            prompt_version="test",
            session_id=a.id,
            trades=[],
        ).model_dump(mode="json"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    assert store.evidence_path(tva_root, a.id).is_file()
    _four_on(monkeypatch)
    build_day(tva_root, DAY, executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, DAY)
    assert payload is not None
    by_id = {row["session_id"]: row for row in payload["clips"]}
    assert by_id[a.id]["cascade"] == "unchanged"
    assert store.evidence_path(tva_root, a.id).is_file()
    session = _rules(tva_root, a.id)
    for rule in DAY_RULE_IDS:
        assert session[rule]["status"] == "unverifiable"
        assert session[rule]["reason"] == day_rule_reason(DAY)
    assert _day_rules(tva_root)["R-3L30"]["status"] == "violated"
    assert rollup(tva_root).summary.violations.total == 1


def test_m24_bias_never_violated_without_evidence(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    session = _rules(tva_root, a.id)
    assert session["R-BIAS"]["status"] == "unverifiable"
    assert session["R-BIAS"]["reason"] == EVIDENCE_PENDING_REASON
    store.save_insights(
        tva_root,
        a.id,
        Insights(
            provider="fake",
            model="none",
            session_events=[
                SessionEvent(t=10.0, kind="bias_statement", seg="seg_001", text="Bias long"),
            ],
        ),
    )
    rules_session(a.id, root=tva_root)
    again = _rules(tva_root, a.id)
    assert again["R-BIAS"]["status"] == "pass"
    assert again["R-BIAS"]["status"] != "violated"


def test_l0_rules_json_matches_snapshot_with_four_set(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    src = L0_DIR / "l0-ocr"
    dest = config.session_dir(tva_root, L0_SESSION)
    dest.mkdir(parents=True)
    for name in ("session.json", "fills.parquet", "trades.parquet", "evidence.json", "rules.json"):
        shutil.copy2(src / name, dest / name)
    record = store.load_session(tva_root, L0_SESSION)
    write_pause_check(
        tva_root,
        PauseCheck(
            session_id=record.id,
            recording_sha256=record.recording.sha256,
            part_shas=[],
            duration_s=float(record.recording.duration_s),
            pause_check="clear",
            pause_total_s=0.0,
            clock_resolution_s=1,
            pause_detectable_from_s=30.0,
            ocr_provider="injected",
            ocr_model="test",
        ),
    )
    json_files_equal(store.rules_path(tva_root, L0_SESSION), src / "rules.json")
    assert not (tva_root / "days").exists()
    rules_session(L0_SESSION, root=tva_root)
    session = _rules(tva_root, L0_SESSION)
    assert session["R-DLL"]["reason"] == "daily_loss_limit_usd is unset (D4)"
    assert session["R-MAX10"]["status"] == "pass"
    assert session["R-MAX10"]["reason"] != day_rule_reason(date(2026, 5, 14))
    assert session["R-CLOSE"]["reason"] == "flat_by is unset"
    assert session["R-PLAYBOOK"]["status"] == "pass"


def test_tva_rules_legacy_unbuilt_stays_per_session(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flags on without enabled_from/build: treat as legacy, do not invent a day reason (§2.1)."""
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    src = L0_DIR / "l0-ocr"
    shutil.copy2(src / "trades.parquet", store.trades_path(tva_root, a.id))
    shutil.copy2(src / "evidence.json", store.evidence_path(tva_root, a.id))
    rules_session(a.id, root=tva_root)
    session = _rules(tva_root, a.id)
    assert session["R-MAX10"]["reason"] != day_rule_reason(DAY)
    assert session["R-MAX10"]["status"] == "pass"
    assert not (tva_root / "days").exists()


def test_tva_rules_writes_both_or_stale(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    pointer = (tva_root / "days" / DAY.isoformat() / "current").read_text(encoding="utf-8").strip()
    day_rules = tva_root / "days" / DAY.isoformat() / "builds" / pointer / "rules.json"
    assert day_rules.is_file()
    rules_session(a.id, root=tva_root)
    assert store.rules_path(tva_root, a.id).is_file()
    assert day_rules.is_file()
    _session(
        tva_root,
        "2026-09-14_150000",
        start=datetime(2026, 9, 14, 15, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    with pytest.raises(DayStale):
        rules_session(a.id, root=tva_root)


def test_confirmed_proposal_remap_and_discard(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    first = _write_csv(
        tmp_path / "a.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=first, venue="amp")
    store.write_json(
        store.proposals_path(tva_root, a.id),
        IntentProposals(
            session_id=a.id,
            proposals=[
                IntentProposal(tva_trade_id="T01", proposed_tags=["3C"], status="confirmed")
            ],
        ).model_dump(mode="json"),
    )
    shifted = _write_csv(
        tmp_path / "b.csv",
        [
            *_win("2026-09-14T07:01:00+0000", "2026-09-14T07:02:00+0000", spread="early"),
            *_win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
        ],
    )
    build_day(tva_root, DAY, executions=shifted, venue="amp")
    payload = load_current_day_json(tva_root, DAY)
    assert payload is not None
    remaps = payload.get("proposal_remaps") or []
    assert any(item.get("old_tva_trade_id") == "T01" and item.get("new_tva_trade_id") == "T02" for item in remaps)
    gone = _write_csv(
        tmp_path / "c.csv",
        _win("2026-09-14T07:01:00+0000", "2026-09-14T07:02:00+0000", spread="early"),
    )
    build_day(tva_root, DAY, executions=gone, venue="amp")
    again = load_current_day_json(tva_root, DAY)
    assert again is not None
    discarded = again.get("proposals_discarded") or []
    assert discarded


def test_drop_day_command_removes_day_rows(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    _two_clips(tva_root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    assert _ledger_day(tva_root)
    assert main(["--root", str(tva_root), "ledger", "drop-day", DAY.isoformat()]) == 0
    assert _ledger_day(tva_root) == []
    drop_day_rows(tva_root, DAY)


def test_invalid_clip_keeps_alignment_invalid(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a, _b = _two_clips(tva_root)
    store.save_session(
        tva_root,
        a.model_copy(
            update={
                "alignment": Alignment(
                    offset_s=0.0,
                    drift_s_per_h=0.0,
                    confidence=0.0,
                    method="invalid",
                )
            }
        ),
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    session = _rules(tva_root, a.id)
    for rule in ("R-HOURLY", "R-ZONE", "R-TILT", "R-3C-CT", "R-ARRIVAL"):
        assert session[rule]["reason"] == "alignment_invalid"
