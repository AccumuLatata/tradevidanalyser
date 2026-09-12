from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.rules import (
    EVIDENCE_BACKED_RULE_IDS,
    EVIDENCE_PRE_S,
    SLTP_REASON,
    TILT_COOLDOWN_S,
    RuleTrade,
    evaluate_rules,
    rules_session,
)
from tradevidanalyser.schema import (
    Alignment,
    Evidence,
    EvidenceTrade,
    EvidenceWindow,
    Insights,
    RecordingInfo,
    SessionEvent,
    SessionRecord,
    StatedCite,
    StatedFields,
)

T0 = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)

START = datetime(2026, 3, 10, 13, 0, tzinfo=timezone.utc)


def _cite(value: str, seg: str) -> StatedCite:
    return StatedCite(value=value, seg=seg)


def _et(
    trade_id: str = "T01",
    *,
    t0: float = 0.0,
    t1: float = 400.0,
    commentary: list[str] | None = None,
    stated: StatedFields | None = None,
    alignment: str | None = None,
    confidence: float = 0.96,
) -> EvidenceTrade:
    return EvidenceTrade(
        tva_trade_id=trade_id,
        window=EvidenceWindow(t0=t0, t1=t1),
        commentary=commentary if commentary is not None else ["seg_001"],
        stated=stated or StatedFields(),
        alignment_confidence=confidence,
        alignment=alignment,  # type: ignore[arg-type]
    )


def _evidence(*trades: EvidenceTrade, session_id: str = "2026-03-10_130000") -> Evidence:
    return Evidence(
        provider="fake",
        model="none",
        prompt_version="stated-v1",
        session_id=session_id,
        trades=list(trades),
    )


def _event(kind: str, t: float, seg: str, text: str = "") -> SessionEvent:
    return SessionEvent(t=t, kind=kind, seg=seg, text=text)  # type: ignore[arg-type]


def _by_id(
    evidence: Evidence | None = None,
    events: list[SessionEvent] | None = None,
    *,
    session_start: datetime | None = START,
    alignment: Alignment | None = None,
):
    return {
        item.rule: item
        for item in evaluate_rules(
            [],
            evidence=evidence,
            session_events=events,
            session_start=session_start,
            alignment=alignment,
        )
    }


def _assert_pass_cites(check) -> None:
    assert check.status == "pass"
    segs = check.evidence.get("segs") or []
    assert segs, f"{check.rule} passed without a cited segment"


def test_absent_speech_is_never_pass() -> None:
    silent = _evidence(_et(commentary=[], stated=StatedFields()))
    checks = _by_id(silent, [])
    for rule in EVIDENCE_BACKED_RULE_IDS:
        assert checks[rule].status == "unverifiable"
        assert checks[rule].status != "pass"


def test_no_evidence_file_is_unverifiable() -> None:
    checks = _by_id(None, None)
    for rule in EVIDENCE_BACKED_RULE_IDS:
        assert checks[rule].status == "unverifiable"


def test_r_playbook_pass_and_on_the_fly() -> None:
    named = _evidence(
        _et(stated=StatedFields(playbook=_cite("IB fade", "seg_010"), setup=_cite("ONH", "seg_011")))
    )
    check = _by_id(named)["R-PLAYBOOK"]
    _assert_pass_cites(check)
    assert "seg_010" in check.evidence["segs"]

    fly = _evidence(_et(commentary=["seg_002"]))
    check = _by_id(fly)["R-PLAYBOOK"]
    assert check.status == "violated"
    assert "T01" in check.evidence["trade_ids"]


def test_r_defined_needs_stop_and_target() -> None:
    both = _evidence(
        _et(
            stated=StatedFields(
                stop_raw=_cite("unter ONL", "seg_020"),
                target_raw=_cite("ONH", "seg_021"),
            )
        )
    )
    _assert_pass_cites(_by_id(both)["R-DEFINED"])

    stop_only = _evidence(_et(stated=StatedFields(stop_raw=_cite("unter ONL", "seg_020"))))
    assert _by_id(stop_only)["R-DEFINED"].status == "violated"


def test_r_3c_ct_table() -> None:
    ok = _evidence(_et(stated=StatedFields(setup=_cite("3c long", "seg_030"))))
    _assert_pass_cites(_by_id(ok)["R-3C-CT"])

    events = [_event("rule_mention", 10, "seg_031", "gegen den Trend")]
    bad = _evidence(_et(commentary=["seg_001"]))
    check = _by_id(bad, events)["R-3C-CT"]
    assert check.status == "violated"

    other = _evidence(_et(stated=StatedFields(setup=_cite("IB fade", "seg_032"))))
    assert _by_id(other)["R-3C-CT"].status == "unverifiable"


def test_r_arrival_cue() -> None:
    events = [_event("rule_mention", 20, "seg_040", "warte auf Kerzenschluss")]
    ev = _evidence(_et(commentary=["seg_001"]))
    _assert_pass_cites(_by_id(ev, events)["R-ARRIVAL"])

    no_cue = _evidence(_et(commentary=["seg_001"]))
    assert _by_id(no_cue, [])["R-ARRIVAL"].status == "violated"


def test_r_sltp_always_unverifiable() -> None:
    ev = _evidence(
        _et(
            stated=StatedFields(
                playbook=_cite("IB fade", "seg_010"),
                stop_raw=_cite("ONL", "seg_020"),
                target_raw=_cite("ONH", "seg_021"),
            )
        )
    )
    events = [
        _event("hourly_checkin", 50 * 60, "seg_050", "check in"),
        _event("bias_statement", 12, "seg_012", "long bias"),
    ]
    check = _by_id(ev, events)["R-SLTP"]
    assert check.status == "unverifiable"
    assert check.reason == SLTP_REASON


def test_r_hourly_near_xx50() -> None:
    ev = _evidence(_et())
    near = [_event("hourly_checkin", 50 * 60, "seg_050", "Stunden-Check")]
    _assert_pass_cites(_by_id(ev, near)["R-HOURLY"])

    far = [_event("hourly_checkin", 20 * 60, "seg_051", "zu früh")]
    assert _by_id(ev, far)["R-HOURLY"].status == "violated"

    assert _by_id(ev, [])["R-HOURLY"].status == "violated"


def test_r_zone_overlap() -> None:
    ev = _evidence(_et(t0=0, t1=400))
    clear = [_event("no_trade_zone", 800, "seg_060", "keine Trades")]
    _assert_pass_cites(_by_id(ev, clear)["R-ZONE"])

    hit = [_event("no_trade_zone", 120, "seg_061", "keine Trades")]
    assert _by_id(ev, hit)["R-ZONE"].status == "violated"

    speech_only = _evidence(_et())
    assert _by_id(speech_only, [])["R-ZONE"].status == "unverifiable"


def test_r_bias_from_event_or_stated() -> None:
    ev = _evidence(_et(stated=StatedFields(bias=_cite("long", "seg_070"))))
    _assert_pass_cites(_by_id(ev)["R-BIAS"])

    events = [_event("bias_statement", 15, "seg_071", "bias bleibt long")]
    _assert_pass_cites(_by_id(_evidence(_et()), events)["R-BIAS"])

    assert _by_id(_evidence(_et()), [])["R-BIAS"].status == "violated"


def test_r_tilt_cooldown() -> None:
    # window t0=0 → entry at 180; tilt at 50 → gap 130 < 300
    soon = _evidence(_et(t0=0, t1=400))
    tilt = [_event("tilt", 50, "seg_080", "TILT")]
    assert _by_id(soon, tilt)["R-TILT"].status == "violated"

    # entry at t0+180 = 600; tilt at 50 → gap 550 > 300
    later = _evidence(_et(t0=420, t1=800))
    _assert_pass_cites(_by_id(later, tilt)["R-TILT"])

    assert _by_id(_evidence(_et()), [])["R-TILT"].status == "unverifiable"
    assert EVIDENCE_PRE_S == 180.0
    assert TILT_COOLDOWN_S == 300.0


def test_low_alignment_keeps_timing_rules_unverifiable() -> None:
    ev = _evidence(_et(alignment="low", confidence=0.4))
    events = [
        _event("hourly_checkin", 50 * 60, "seg_050", "check"),
        _event("no_trade_zone", 800, "seg_060", "zone"),
        _event("tilt", 50, "seg_080", "TILT"),
    ]
    clock = Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=0.4,
        method="filename",
    )
    checks = _by_id(ev, events, alignment=clock)
    assert checks["R-HOURLY"].status == "unverifiable"
    assert checks["R-ZONE"].status == "unverifiable"
    assert checks["R-TILT"].status == "unverifiable"


def test_evidence_backed_pass_always_cites_a_segment() -> None:
    ev = _evidence(
        _et(
            t0=420,
            t1=800,
            stated=StatedFields(
                playbook=_cite("IB fade", "seg_010"),
                setup=_cite("ONH", "seg_011"),
                bias=_cite("long", "seg_070"),
                stop_raw=_cite("ONL", "seg_020"),
                target_raw=_cite("ONH", "seg_021"),
            ),
        )
    )
    events = [
        _event("rule_mention", 430, "seg_040", "Kerzenschluss und 3c"),
        _event("hourly_checkin", 50 * 60, "seg_050", "check in"),
        _event("no_trade_zone", 80, "seg_060", "keine Trades"),
        _event("bias_statement", 15, "seg_071", "bias long"),
        _event("tilt", 50, "seg_080", "TILT"),
    ]
    checks = _by_id(ev, events)
    for rule in EVIDENCE_BACKED_RULE_IDS:
        check = checks[rule]
        if check.status == "pass":
            assert check.evidence.get("segs"), f"{rule} pass missing segs"
        assert check.status != "pass" or check.evidence.get("segs")


def _session(root: Path, session_id: str = "2026-09-11_143000") -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-09-11T14:30:00+02:00",
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _trade(n: int, *, entry_s: float, exit_s: float, pnl: float) -> RuleTrade:
    return RuleTrade(
        tva_trade_id=f"T{n:02d}",
        instrument="MNQ",
        direction="long",
        entry_timestamp=T0 + timedelta(seconds=entry_s),
        exit_timestamp=T0 + timedelta(seconds=exit_s),
        entry_price=21000.0,
        exit_price=21001.0,
        stop_price=None,
        hold_seconds=60.0,
        net_pnl_currency=pnl,
        gross_pnl_currency=None,
        status="closed",
    )


def _write_trades(root: Path, session_id: str, trades: list[RuleTrade]) -> None:
    table = pa.table(
        {
            "tva_trade_id": [t.tva_trade_id for t in trades],
            "instrument": [t.instrument for t in trades],
            "direction": [t.direction for t in trades],
            "entry_timestamp": pa.array(
                [t.entry_timestamp for t in trades], type=pa.timestamp("us", tz="UTC")
            ),
            "exit_timestamp": pa.array(
                [t.exit_timestamp for t in trades], type=pa.timestamp("us", tz="UTC")
            ),
            "entry_price": [t.entry_price for t in trades],
            "exit_price": [t.exit_price for t in trades],
            "stop_price": [t.stop_price for t in trades],
            "hold_seconds": [t.hold_seconds for t in trades],
            "net_pnl_currency": [t.net_pnl_currency for t in trades],
            "gross_pnl_currency": [t.gross_pnl_currency for t in trades],
            "status": [t.status for t in trades],
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def test_cli_rules_reads_evidence_and_events(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, [_trade(1, entry_s=0, exit_s=60, pnl=5)])
    store.write_json(
        store.evidence_path(tva_root, record.id),
        _evidence(
            _et(
                stated=StatedFields(
                    playbook=_cite("IB fade", "seg_010"),
                    stop_raw=_cite("ONL", "seg_020"),
                    target_raw=_cite("ONH", "seg_021"),
                )
            ),
            session_id=record.id,
        ).model_dump(mode="json"),
    )
    store.save_insights(
        tva_root,
        record.id,
        Insights(
            provider="fake",
            model="none",
            session_events=[
                _event("hourly_checkin", 20 * 60, "seg_050", "Stunden-Check"),
                _event("bias_statement", 12, "seg_012", "long bias"),
            ],
        ),
    )
    assert main(["--root", str(tva_root), "rules", record.id]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    report = store.read_json(store.rules_path(tva_root, record.id))
    by_id = {row["rule"]: row for row in report["rules"]}
    assert by_id["R-PLAYBOOK"]["status"] == "pass"
    assert by_id["R-PLAYBOOK"]["evidence"]["segs"]
    assert by_id["R-DEFINED"]["status"] == "pass"
    assert by_id["R-SLTP"]["status"] == "unverifiable"
    assert by_id["R-HOURLY"]["status"] == "pass"
    first = store.rules_path(tva_root, record.id).read_text(encoding="utf-8")
    assert rules_session(record.id, root=tva_root).status == "ok"
    assert store.rules_path(tva_root, record.id).read_text(encoding="utf-8") == first


@pytest.mark.golden
def test_golden_scorecard_pass_cites_segment() -> None:
    golden = config.golden_dir(config.resolve_root())
    if not golden.is_dir():
        pytest.skip("golden excerpt absent")
    paths = list(golden.rglob("rules.json"))
    if not paths:
        pytest.skip("no golden rules.json")
    for path in paths:
        report = store.read_json(path)
        for row in report.get("rules", []):
            if row.get("rule") not in EVIDENCE_BACKED_RULE_IDS:
                continue
            if row.get("status") != "pass":
                continue
            segs = (row.get("evidence") or {}).get("segs") or []
            assert segs, f"{path} {row.get('rule')} passed without a cited segment"
