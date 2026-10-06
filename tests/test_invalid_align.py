from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.align import align_session
from tradevidanalyser.cli import main
from tradevidanalyser.evidence import effective_alignment, evidence_session
from tradevidanalyser.rules import CLOCK_MAPPED_RULES, evaluate_rules
from tradevidanalyser.schema import (
    ALIGN_INVALID_ERROR,
    ALIGNMENT_INVALID_REASON,
    Alignment,
    Evidence,
    RecordingInfo,
    SessionEvent,
    SessionRecord,
    alignment_is_invalid,
)

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_VARIANTS = ("l0-ocr", "l0-filename")
L0_SESSION_FILES = (
    "session.json",
    "fills.parquet",
    "trades.parquet",
    "evidence.json",
    "rules.json",
    "debrief.md",
    "debrief.json",
)
L0_JSON_DROP = frozenset({"app_version", "log_line", "created"})
VIENNA = timezone(timedelta(hours=2))
START = datetime(2026, 9, 11, 14, 30, tzinfo=VIENNA)


def _invalid_alignment() -> Alignment:
    return Alignment(
        offset_s=0.0,
        drift_s_per_h=0.0,
        confidence=0.0,
        method="invalid",
        samples=[],
    )


def _session(root: Path, *, method: str = "invalid") -> SessionRecord:
    alignment = (
        _invalid_alignment()
        if method == "invalid"
        else Alignment(
            offset_s=0.0,
            drift_s_per_h=0.0,
            confidence=0.96,
            method=method,  # type: ignore[arg-type]
        )
    )
    record = SessionRecord(
        id="2026-09-11_143000",
        recording=RecordingInfo(
            path="recordings/2026-09-11 14-30-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=START.isoformat(),
            duration_s=3600.0,
            filename="2026-09-11 14-30-00.mp4",
        ),
        alignment=alignment,
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_trades(root: Path, session_id: str) -> None:
    table = pa.table(
        {
            "tva_trade_id": pa.array(["T01"], type=pa.string()),
            "entry_timestamp": pa.array(
                [START.astimezone(timezone.utc) + timedelta(seconds=200)],
                type=pa.timestamp("us", tz="UTC"),
            ),
            "exit_timestamp": pa.array(
                [START.astimezone(timezone.utc) + timedelta(seconds=220)],
                type=pa.timestamp("us", tz="UTC"),
            ),
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _drop_l0(payload: object) -> object:
    if isinstance(payload, dict):
        return {key: _drop_l0(value) for key, value in payload.items() if key not in L0_JSON_DROP}
    if isinstance(payload, list):
        return [_drop_l0(item) for item in payload]
    return payload


def _canonical(payload: object) -> str:
    return json.dumps(_drop_l0(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def test_invalid_alignment_round_trip(tva_root: Path) -> None:
    record = _session(tva_root)
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "invalid"
    assert loaded.alignment.offset_s == 0.0
    assert loaded.alignment.drift_s_per_h == 0.0
    assert loaded.alignment.confidence == 0.0
    store.save_session(tva_root, loaded)
    again = store.load_session(tva_root, record.id)
    assert again.alignment == loaded.alignment
    raw = store.read_json(store.session_json_path(tva_root, record.id))
    assert raw["alignment"]["method"] == "invalid"


def test_effective_alignment_does_not_map_invalid_to_filename(tva_root: Path) -> None:
    record = _session(tva_root)
    clock = effective_alignment(record)
    assert clock.method == "invalid"
    assert alignment_is_invalid(clock)
    assert clock.method != "filename"


def test_invalid_session_has_no_evidence_windows(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id)
    store.write_json(
        store.evidence_path(tva_root, record.id),
        Evidence(provider="fake", model="none", prompt_version="x", session_id=record.id).model_dump(
            mode="json"
        ),
    )
    assert store.evidence_path(tva_root, record.id).is_file()
    result = evidence_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert result.reason == ALIGNMENT_INVALID_REASON
    assert result.trades == 0
    assert not store.evidence_path(tva_root, record.id).is_file()
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "invalid"


def test_align_refuses_to_overwrite_invalid(tva_root: Path) -> None:
    record = _session(tva_root)
    session_path = store.session_json_path(tva_root, record.id)
    before = session_path.read_text(encoding="utf-8")
    before_mtime = session_path.stat().st_mtime_ns
    evidence = store.evidence_path(tva_root, record.id)
    evidence.write_text("{}", encoding="utf-8")
    evidence_mtime = evidence.stat().st_mtime_ns
    with pytest.raises(ValueError, match=ALIGN_INVALID_ERROR):
        align_session(record.id, root=tva_root)
    with pytest.raises(ValueError, match=ALIGN_INVALID_ERROR):
        align_session(record.id, root=tva_root, manual_offset=3.5)
    assert session_path.read_text(encoding="utf-8") == before
    assert session_path.stat().st_mtime_ns == before_mtime
    assert evidence.is_file()
    assert evidence.stat().st_mtime_ns == evidence_mtime
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "invalid"


def test_cli_align_invalid_writes_nothing(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    session_path = store.session_json_path(tva_root, record.id)
    before = session_path.read_bytes()
    assert main(["--root", str(tva_root), "align", record.id]) == 1
    out = capsys.readouterr().out
    assert ALIGN_INVALID_ERROR in out
    assert session_path.read_bytes() == before
    assert store.load_session(tva_root, record.id).alignment.method == "invalid"


def test_clock_mapped_rules_are_alignment_invalid() -> None:
    alignment = _invalid_alignment()
    events = (
        SessionEvent(t=10.0, kind="bias_statement", seg="seg_001", text="Bias long"),
        SessionEvent(t=50.0, kind="hourly_checkin", seg="seg_002", text="Check-in"),
        SessionEvent(t=80.0, kind="tilt", seg="seg_003", text="Tilt"),
    )
    checks = {
        item.rule: item
        for item in evaluate_rules([], alignment=alignment, session_events=events)
    }
    for rule in CLOCK_MAPPED_RULES:
        assert checks[rule].status == "unverifiable"
        assert checks[rule].reason == ALIGNMENT_INVALID_REASON
    assert checks["R-BIAS"].reason != ALIGNMENT_INVALID_REASON
    assert checks["R-SLTP"].reason != ALIGNMENT_INVALID_REASON
    assert checks["R-PLAYBOOK"].reason != ALIGNMENT_INVALID_REASON


def test_filename_alignment_is_not_treated_as_invalid(tva_root: Path) -> None:
    record = _session(tva_root, method="filename")
    assert not alignment_is_invalid(record.alignment)
    clock = effective_alignment(record)
    assert clock.method == "filename"
    align_session(record.id, root=tva_root)
    loaded = store.load_session(tva_root, record.id)
    assert loaded.alignment is not None
    assert loaded.alignment.method == "filename"


def test_l0_round_trip_matches_snapshot(tva_root: Path) -> None:
    for name in L0_VARIANTS:
        src = L0_DIR / name
        raw = json.loads((src / "session.json").read_text(encoding="utf-8"))
        session_id = raw["id"]
        dest = config.session_dir(tva_root, session_id)
        dest.mkdir(parents=True, exist_ok=True)
        for filename in L0_SESSION_FILES:
            shutil.copy2(src / filename, dest / filename)
        before = {path.name: path.read_bytes() for path in dest.iterdir()}
        record = store.load_session(tva_root, session_id)
        assert record.alignment is not None
        assert record.alignment.method in {"ocr_clock", "filename"}
        assert not alignment_is_invalid(record.alignment)
        clock = effective_alignment(record)
        assert clock.method == record.alignment.method
        store.save_session(tva_root, record)
        after = json.loads(store.session_json_path(tva_root, session_id).read_text(encoding="utf-8"))
        assert _canonical(after) == _canonical(raw)
        for filename, blob in before.items():
            if filename == "session.json":
                continue
            assert (dest / filename).read_bytes() == blob
        if name == "l0-ocr":
            evidence = json.loads((src / "evidence.json").read_text(encoding="utf-8"))
            for trade in evidence["trades"]:
                assert trade.get("alignment") is None
