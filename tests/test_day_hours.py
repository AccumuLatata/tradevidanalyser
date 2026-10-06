from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.day_hours import (
    BASIS_FILL_SPAN,
    BASIS_MIXED,
    REASON_MIXED,
    REASON_OUTSIDE,
    REASON_PAUSED,
    REASON_SPAN_ZERO_EXCLUDED,
)
from tradevidanalyser.day_manifest import build_day
from tradevidanalyser.flags import (
    ENV_DAY_MANIFEST,
    ENV_DAY_PUBLISH,
    ENV_DAY_RULES,
    ENV_EXCLUSIVE_FILLS,
    ENV_PAUSE_GUARD,
    ENV_TRADING_HOURS,
    trading_hours_enabled,
)
from tradevidanalyser.ledger import add_session, build_summary, ledger_db_path, rollup
from tradevidanalyser.pause_guard import PauseCheck, write_pause_check
from tradevidanalyser.schema import Alignment, RecordingInfo, SessionRecord

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


def _suspect(root: Path, record: SessionRecord) -> SessionRecord:
    write_pause_check(
        root,
        PauseCheck(
            session_id=record.id,
            recording_sha256=record.recording.sha256,
            part_shas=[],
            duration_s=float(record.recording.duration_s),
            pause_check="suspected",
            pause_total_s=300.0,
            clock_resolution_s=1,
            pause_detectable_from_s=30.0,
            ocr_provider="injected",
            ocr_model="test",
        ),
    )
    updated = record.model_copy(
        update={
            "alignment": Alignment(
                offset_s=0.0,
                drift_s_per_h=0.0,
                confidence=0.0,
                method="invalid",
                samples=[],
            )
        }
    )
    store.save_session(root, updated)
    return updated


def _csv_row(ts: str, *, side: str, spread: str, price: str = "21000.0") -> str:
    return (
        f"{ts},MNQM26,{side},USD,MNQ,future,{price},1.0,0,0,N/A,N/A,,,{spread}"
    )


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _win(entry: str, exit_ts: str, *, spread: str) -> list[str]:
    return [
        _csv_row(entry, side="buy", spread=spread),
        _csv_row(exit_ts, side="sell", spread=spread, price="21001.0"),
    ]


def _session_hours(root: Path, session_id: str) -> float:
    con = duckdb.connect(str(ledger_db_path(root)), read_only=True)
    try:
        row = con.execute(
            "SELECT hours FROM sessions WHERE session_id = ?", [session_id]
        ).fetchone()
    finally:
        con.close()
    assert row is not None
    return float(row[0])


def test_trading_hours_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_TRADING_HOURS, raising=False)
    assert trading_hours_enabled() is False
    for raw in ("", "0", "false", "off", "no"):
        monkeypatch.setenv(ENV_TRADING_HOURS, raw)
        assert trading_hours_enabled() is False


def test_l0_rate_identity_keys_absent_with_four_set(
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
    add_session(L0_SESSION, root=tva_root)
    assert not (tva_root / "days").exists()
    summary = build_summary(tva_root, week="2026-W20")
    dumped = summary.model_dump(mode="json")
    want = json.loads((src / "ledger.json").read_text(encoding="utf-8"))
    sess = want["sessions"][0]
    assert summary.trades == sess["trade_count"]
    assert summary.hours == pytest.approx(sess["hours"])
    assert summary.trades_per_hour == pytest.approx(sess["trade_count"] / sess["hours"])
    assert "trades_per_hour_reason" not in dumped
    assert "hours_basis" not in dumped
    assert "trades_span_zero" not in dumped
    period = dumped["periods"][0]
    assert "trades_per_hour_reason" not in period
    assert "hours_basis" not in period
    assert _session_hours(tva_root, L0_SESSION) == pytest.approx(
        float(record.recording.duration_s) / 3600.0
    )


def test_m1_rate_counter_counts_each_trade_once(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    _session(
        tva_root,
        "2026-09-14_130000",
        start=datetime(2026, 9, 14, 13, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
            *_win("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b"),
            *_win("2026-09-14T11:05:00+0000", "2026-09-14T11:06:00+0000", spread="c"),
            *_win("2026-09-14T08:30:00+0000", "2026-09-14T08:31:00+0000", spread="out"),
        ],
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    assert summary.trades == 3
    assert summary.hours == pytest.approx(180.0 / 3600.0)
    assert summary.trades_per_hour == pytest.approx(3 / (180.0 / 3600.0))
    assert dumped["trades_per_hour_reason"] == REASON_OUTSIDE
    assert dumped["hours_basis"] == BASIS_FILL_SPAN
    assert dumped["trades_outside_clips"] == 1
    assert _session_hours(tva_root, a.id) == pytest.approx(600.0 / 3600.0)


def test_m5_paused_clip_rate_null_not_duration(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    record = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _suspect(tva_root, record)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    duration_rate = 1 / (600.0 / 3600.0)
    assert summary.hours == 0.0
    assert summary.trades == 0
    assert summary.trades_per_hour is None
    assert summary.trades_per_hour != duration_rate
    assert dumped["trades_per_hour_reason"] == REASON_PAUSED
    assert dumped["hours_basis"] == BASIS_FILL_SPAN
    assert dumped["trades_outside_clips"] == 1
    assert _session_hours(tva_root, record.id) == pytest.approx(600.0 / 3600.0)


def test_m7_outside_not_in_counter(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
            *_win("2026-09-14T08:30:00+0000", "2026-09-14T08:31:00+0000", spread="gap"),
        ],
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    assert summary.trades == 1
    assert summary.hours == pytest.approx(60.0 / 3600.0)
    assert dumped["trades_per_hour_reason"] == REASON_OUTSIDE
    assert dumped["hours_basis"] == BASIS_FILL_SPAN
    assert dumped["hours_basis"] != BASIS_MIXED
    assert dumped["trades_outside_clips"] == 1
    assert _session_hours(tva_root, a.id) == pytest.approx(600.0 / 3600.0)


def test_m18_mixed_basis(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    legacy = _session(
        tva_root,
        "2026-09-16_143000",
        start=datetime(2026, 9, 16, 14, 30, tzinfo=VIENNA),
        duration_s=7200.0,
        sha256="d" * 64,
    )
    table = pa.table(
        {
            "tva_trade_id": ["T01", "T02"],
            "trade_id": ["jt-1", "jt-2"],
            "entry_fill_id": ["fill-a", "fill-b"],
            "direction": ["long", "short"],
            "instrument": ["MNQ", "MNQ"],
            "entry_price": [21000.0, 21010.0],
            "exit_price": [20990.0, 21020.0],
            "net_pnl_currency": [-10.0, 10.0],
            "status": ["closed", "closed"],
            "venue": ["amp", "amp"],
        }
    )
    pq.write_table(table, store.trades_path(tva_root, legacy.id))
    add_session(legacy.id, root=tva_root)
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    assert dumped["hours_basis"] == BASIS_MIXED
    assert dumped["trades_per_hour_reason"] == REASON_MIXED
    assert summary.trades == 3
    assert summary.hours == pytest.approx(60.0 / 3600.0 + 2.0)
    assert _session_hours(tva_root, legacy.id) == pytest.approx(2.0)


def test_m19_span_zero_excluded(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    b = _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            _csv_row("2026-09-14T07:05:00+0000", side="buy", spread="cross"),
            _csv_row("2026-09-14T09:01:00+0000", side="sell", spread="cross", price="21001.0"),
            *_win("2026-09-14T09:02:00+0000", "2026-09-14T09:03:00+0000", spread="b"),
        ],
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    assert summary.trades == 1
    assert dumped["trades_span_zero"] == 1
    assert dumped["trades_per_hour_reason"] == REASON_SPAN_ZERO_EXCLUDED
    assert dumped["hours_basis"] == BASIS_FILL_SPAN
    assert summary.hours == pytest.approx(120.0 / 3600.0)
    assert _session_hours(tva_root, b.id) == pytest.approx(600.0 / 3600.0)


def test_flag_off_ignores_day_rollups_hours(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    for name in (
        ENV_EXCLUSIVE_FILLS,
        ENV_DAY_RULES,
        ENV_DAY_PUBLISH,
        ENV_TRADING_HOURS,
        ENV_PAUSE_GUARD,
        ENV_DAY_MANIFEST,
    ):
        monkeypatch.delenv(name, raising=False)
    summary = build_summary(tva_root, week="2026-W38")
    dumped = summary.model_dump(mode="json")
    assert "hours_basis" not in dumped
    assert "trades_per_hour_reason" not in dumped
    assert summary.hours == pytest.approx(_session_hours(tva_root, a.id) * 2)
    result = rollup(tva_root, week="2026-W38")
    assert "hours_basis" not in result.as_dict()
