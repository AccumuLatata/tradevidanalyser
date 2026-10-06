from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from tradevidanalyser import config, store
from tradevidanalyser.day_fills import (
    NEAR_BOUNDARY_EPS_S,
    clip_has_core,
    core_interval,
)
from tradevidanalyser.day_manifest import (
    DayManifestError,
    build_day,
    day_state,
    load_current_day_json,
    nominal_end_utc,
    nominal_start_utc,
)
from tradevidanalyser.fills import FillsError
from tradevidanalyser.flags import (
    ENV_DAY_MANIFEST,
    ENV_DAY_PUBLISH,
    ENV_DAY_RULES,
    ENV_EXCLUSIVE_FILLS,
    ENV_PAUSE_GUARD,
    ENV_TRADING_HOURS,
)
from tradevidanalyser.ledger import ledger_db_path
from tradevidanalyser.pause_guard import PauseCheck, write_pause_check
from tradevidanalyser.pipeline import fills_session
from tradevidanalyser.schema import (
    Alignment,
    Evidence,
    RecordingInfo,
    SessionRecord,
)

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_SYNTHETIC = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
L0_SESSION = "2026-05-14_160300"
VIENNA = timezone(timedelta(hours=2))

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
    filename: str | None = None,
    alignment: Alignment | None = None,
) -> SessionRecord:
    name = filename or f"{session_id[:10]} {session_id[11:13]}-{session_id[13:15]}-{session_id[15:17]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{name}",
            sha256=sha256 or ("a" * 64),
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename=name,
        ),
        alignment=alignment,
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    _clear(root, record)
    return record


def _csv_row(
    ts: str,
    *,
    side: str = "buy",
    price: str = "21000.0",
    spread: str = "g1",
    notes: str = "",
) -> str:
    return (
        f"{ts},MNQM26,{side},USD,MNQ,future,{price},1.0,"
        f"0,0,N/A,N/A,,{notes},{spread}"
    )


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _round_turn(entry: str, exit_ts: str, *, spread: str, price: str = "21000.0") -> list[str]:
    return [
        _csv_row(entry, side="buy", price=price, spread=spread),
        _csv_row(exit_ts, side="sell", price=str(float(price) + 1), spread=spread),
    ]


def _write_evidence(root: Path, session_id: str) -> Path:
    path = store.evidence_path(root, session_id)
    store.write_json(
        path,
        Evidence(
            provider="fake",
            model="none",
            prompt_version="test",
            session_id=session_id,
            trades=[],
        ).model_dump(mode="json"),
    )
    return path


def _write_rules(root: Path, session_id: str) -> Path:
    path = store.rules_path(root, session_id)
    store.write_json(path, {"schema_version": "1", "session_id": session_id, "rules": []})
    return path


def _clear(root: Path, record: SessionRecord) -> None:
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


def _suspect(root: Path, record: SessionRecord) -> None:
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
    store.save_session(
        root,
        record.model_copy(
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


def _identities(root: Path, session_id: str) -> set[tuple[str, str, float]]:
    path = store.fills_path(root, session_id)
    if not path.is_file():
        return set()
    rows = pq.read_table(path).select(["timestamp", "side", "price"]).to_pylist()
    out: set[tuple[str, str, float]] = set()
    for row in rows:
        ts = row["timestamp"]
        if hasattr(ts, "isoformat"):
            text = ts.isoformat()
        else:
            text = str(ts)
        out.add((text, str(row["side"]), float(row["price"])))
    return out


def test_flag_off_keeps_legacy_window(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_EXCLUSIVE_FILLS, raising=False)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    _session(
        tva_root,
        "2026-09-14_120000",
        start=datetime(2026, 9, 14, 12, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    monkeypatch.setenv(ENV_DAY_MANIFEST, "1")
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    result = fills_session(a.id, root=tva_root, executions=csv, venue="amp")
    assert result.fills == 2
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["outside"] == []


def test_m1_each_fill_once(
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
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    c = _session(
        tva_root,
        "2026-09-14_130000",
        start=datetime(2026, 9, 14, 13, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
            *_round_turn("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b"),
            *_round_turn("2026-09-14T11:05:00+0000", "2026-09-14T11:06:00+0000", spread="c"),
            *_round_turn("2026-09-14T08:30:00+0000", "2026-09-14T08:31:00+0000", spread="out"),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    ids_a = _identities(tva_root, a.id)
    ids_b = _identities(tva_root, b.id)
    ids_c = _identities(tva_root, c.id)
    assert ids_a and ids_b and ids_c
    assert not (ids_a & ids_b)
    assert not (ids_a & ids_c)
    assert not (ids_b & ids_c)
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    reasons = {item["reason"] for item in payload["outside"]}
    assert "gap" in reasons
    assert payload["trades_outside_clips"] >= 1


def test_m2_half_open_edge(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    start_a = datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA)
    a = _session(tva_root, "2026-09-14_090000", start=start_a, duration_s=600.0)
    start_b = start_a + timedelta(seconds=606)
    b = _session(
        tva_root,
        "2026-09-14_091006",
        start=start_b,
        duration_s=600.0,
        sha256="b" * 64,
        filename="other 2026-09-14 09-10-06.mp4",
    )
    end_a = nominal_end_utc(a)
    start_b_utc = nominal_start_utc(b)
    start_a_utc = nominal_start_utc(a)
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_round_turn(
                end_a.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                (end_a + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                spread="edgeA",
            ),
            *_round_turn(
                start_b_utc.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                (start_b_utc + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                spread="edgeB",
            ),
            *_round_turn(
                start_a_utc.strftime("%Y-%m-%dT%H:%M:%S+0000"),
                (start_a_utc + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
                spread="startA",
            ),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    # M2a: fill at A's end is outside/gap (and near_boundary of A)
    assert any(item["reason"] == "gap" for item in payload["outside"])
    ids_a = _identities(tva_root, a.id)
    ids_b = _identities(tva_root, b.id)
    # M2c start of A → A; M2b start of B → B
    assert any(abs(float(price) - 21000.0) < 1e-9 for _, _, price in ids_a)
    assert any("09:10:06" in ts or "07:10:06" in ts for ts, _, _ in ids_b) or ids_b
    assert not (ids_a & ids_b)


def test_m2d_shared_edge_belongs_to_b(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    start_a = datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA)
    a = _session(tva_root, "2026-09-14_090000", start=start_a, duration_s=600.0)
    start_b = start_a + timedelta(seconds=600)
    b = _session(
        tva_root,
        "2026-09-14_091000",
        start=start_b,
        duration_s=600.0,
        sha256="b" * 64,
        filename="other 2026-09-14 09-10-00.mp4",
    )
    edge = nominal_end_utc(a)
    assert edge == nominal_start_utc(b)
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn(
            edge.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            (edge + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
            spread="edge",
        ),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert not _identities(tva_root, a.id)
    assert _identities(tva_root, b.id)


def test_m5_suspected_claims_nothing(
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
    record = store.load_session(tva_root, record.id)
    assert clip_has_core(record, tva_root) is False
    assert core_interval(record, tva_root) is None
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert not store.fills_path(tva_root, record.id).is_file()
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["outside"]
    assert {item["reason"] for item in payload["outside"]} == {"no_core"}


def test_m7_gap_outside(
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
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T08:30:00+0000", "2026-09-14T08:31:00+0000", spread="gap"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert not _identities(tva_root, a.id)
    assert not _identities(tva_root, b.id)
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert all(item["reason"] == "gap" for item in payload["outside"])
    assert not store.evidence_path(tva_root, a.id).is_file()


def test_m12_after_midnight_stays_on_start_day(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    late = _session(
        tva_root,
        "2026-09-14_230000",
        start=datetime(2026, 9, 14, 23, 0, tzinfo=VIENNA),
        duration_s=3 * 3600.0,
    )
    _session(
        tva_root,
        "2026-09-14_180000",
        start=datetime(2026, 9, 14, 18, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    nxt_a = _session(
        tva_root,
        "2026-09-15_090000",
        start=datetime(2026, 9, 15, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    nxt_b = _session(
        tva_root,
        "2026-09-15_110000",
        start=datetime(2026, 9, 15, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="d" * 64,
    )
    midnight_fill = "2026-09-14T22:30:00+0000"  # 00:30 Vienna on the 15th
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_round_turn(midnight_fill, "2026-09-14T22:31:00+0000", spread="mid"),
            *_round_turn("2026-09-15T07:05:00+0000", "2026-09-15T07:06:00+0000", spread="n"),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    build_day(tva_root, date(2026, 9, 15), executions=csv, venue="amp")
    late_ids = _identities(tva_root, late.id)
    assert late_ids
    assert not any(late_ids & _identities(tva_root, sid) for sid in (nxt_a.id, nxt_b.id))
    nxt = load_current_day_json(tva_root, date(2026, 9, 15))
    assert nxt is not None
    mid_ts = datetime.fromisoformat("2026-09-14T22:30:00+00:00")
    assert not any(
        datetime.fromisoformat(item["timestamp"]) == mid_ts for item in nxt["outside"]
    )


def test_m15_near_boundary_not_owned(
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
    start = nominal_start_utc(a)
    early = start - timedelta(seconds=5)
    assert 5 <= NEAR_BOUNDARY_EPS_S
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn(
            early.strftime("%Y-%m-%dT%H:%M:%S+0000"),
            (early + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S+0000"),
            spread="early",
        ),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert not _identities(tva_root, a.id)
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["outside"][0]["reason"] == "before_first"
    clip = next(row for row in payload["clips"] if row["session_id"] == a.id)
    assert clip["near_boundary"]


def test_csv_sha_alone_does_not_abort_fills(
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
    rows = _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1")
    csv_a = _write_csv(tmp_path / "a.csv", rows)
    csv_b = _write_csv(
        tmp_path / "b.csv",
        [
            _csv_row("2026-09-14T07:05:00+0000", spread="g1", notes="export2"),
            _csv_row("2026-09-14T07:06:00+0000", side="sell", price="21001.0", spread="g1", notes="export2"),
        ],
    )
    assert csv_a.read_bytes() != csv_b.read_bytes()
    build_day(tva_root, date(2026, 9, 14), executions=csv_a, venue="amp")
    result = fills_session(a.id, root=tva_root, executions=csv_b, venue="amp")
    assert result.fills == 2
    other = _write_csv(
        tmp_path / "c.csv",
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1", price="21111.0"),
    )
    with pytest.raises(FillsError, match="Identitätsschlüssel"):
        fills_session(a.id, root=tva_root, executions=other, venue="amp")


def test_cascade_rewrites_ledger_and_deletes_evidence(
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
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    ev_a = _write_evidence(tva_root, a.id)
    ev_b = _write_evidence(tva_root, b.id)
    _write_rules(tva_root, a.id)
    _write_rules(tva_root, b.id)
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            *_round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
            *_round_turn("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b"),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert not ev_a.is_file()
    assert not ev_b.is_file()
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert {row["cascade"] for row in payload["clips"]} == {"changed"}
    con = __import__("duckdb").connect(str(ledger_db_path(tva_root)), read_only=True)
    try:
        sessions = {
            row[0]
            for row in con.execute("SELECT session_id FROM sessions ORDER BY 1").fetchall()
        }
        trades = con.execute("SELECT count(*) FROM trades").fetchone()
        rules = con.execute("SELECT count(*) FROM rule_checks").fetchone()
    finally:
        con.close()
    assert sessions == {a.id, b.id}
    assert trades is not None and trades[0] >= 2
    assert rules is not None and rules[0] == 0
    _write_evidence(tva_root, a.id)
    _write_evidence(tva_root, b.id)
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert store.evidence_path(tva_root, a.id).is_file()
    assert store.evidence_path(tva_root, b.id).is_file()
    again = load_current_day_json(tva_root, date(2026, 9, 14))
    assert again is not None
    assert {row["cascade"] for row in again["clips"]} == {"unchanged"}


def test_l0_parquets_match_with_exclusive_on(
    tva_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    src = L0_DIR / "l0-ocr"
    dest = config.session_dir(tva_root, L0_SESSION)
    dest.mkdir(parents=True)
    shutil.copy2(src / "session.json", dest / "session.json")
    result = fills_session(L0_SESSION, root=tva_root, executions=L0_SYNTHETIC, venue="amp")
    assert result.fills > 0
    got = pq.read_table(store.fills_path(tva_root, L0_SESSION))
    want = pq.read_table(src / "fills.parquet")
    assert got.column_names == want.column_names
    assert got.equals(want, check_metadata=False)
    got_t = pq.read_table(store.trades_path(tva_root, L0_SESSION))
    want_t = pq.read_table(src / "trades.parquet")
    assert got_t.equals(want_t, check_metadata=False)
    assert day_state(tva_root, date(2026, 5, 14)).kind == "legacy_unbuilt"


def test_claimed_by_legacy_neighbor(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DAY_MANIFEST, "1")
    legacy = _session(
        tva_root,
        "2026-09-14_233000",
        start=datetime(2026, 9, 14, 23, 30, tzinfo=VIENNA),
        duration_s=3600.0,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T22:45:00+0000", "2026-09-14T22:46:00+0000", spread="leg"),
    )
    fills_session(legacy.id, root=tva_root, executions=csv, venue="amp")
    assert store.fills_path(tva_root, legacy.id).is_file()
    _four_on(monkeypatch)
    nxt_a = _session(
        tva_root,
        "2026-09-15_090000",
        start=datetime(2026, 9, 15, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    _session(
        tva_root,
        "2026-09-15_110000",
        start=datetime(2026, 9, 15, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="d" * 64,
    )
    build_day(tva_root, date(2026, 9, 15), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 9, 15))
    assert payload is not None
    mid = datetime.fromisoformat("2026-09-14T22:45:00+00:00")
    assert not any(datetime.fromisoformat(item["timestamp"]) == mid for item in payload["outside"])
    claimed = payload["claimed_by_legacy_neighbor"]
    assert claimed
    assert any(datetime.fromisoformat(item["timestamp"]) == mid for item in claimed)
    assert store.fills_path(tva_root, legacy.id).is_file()
    assert not _identities(tva_root, nxt_a.id)


def test_waived_tz_assumption_copied(
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
    audit = tva_root / "days" / "audit.json"
    audit.parent.mkdir(parents=True)
    audit.write_text(json.dumps({"tz_assumption": "waived"}), encoding="utf-8")
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["tz_assumption"] == "waived"


def test_geometric_overlap_without_fill_in_overlap(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=1200.0,
    )
    b = _session(
        tva_root,
        "2026-09-14_091000",
        start=datetime(2026, 9, 14, 9, 10, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
        filename="other 2026-09-14 09-10-00.mp4",
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T07:01:00+0000", "2026-09-14T07:02:00+0000", spread="early"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    by_id = {row["session_id"]: row for row in payload["clips"]}
    assert b.id in by_id[a.id]["overlap"]
    assert a.id in by_id[b.id]["overlap"]


def test_exclusive_build_status_is_not_missing(
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
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    persisted = store.read_json(store.status_path(tva_root, a.id))
    assert persisted["day_state"] == "current_incomplete"
    assert store.compute_status(tva_root, a.id).day_state == "current_incomplete"


def test_cross_clip_round_turn_is_one_trade(
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
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        [
            _csv_row("2026-09-14T07:05:00+0000", side="buy", spread="ab"),
            _csv_row("2026-09-14T09:05:00+0000", side="sell", price="21001.0", spread="ab"),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=csv, venue="amp")
    assert store.trades_path(tva_root, a.id).is_file()
    assert not store.trades_path(tva_root, b.id).is_file()
    rows = pq.read_table(store.trades_path(tva_root, a.id)).to_pylist()
    assert len(rows) == 1
    assert rows[0]["tva_trade_id"] == "T01"
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["trades_outside_clips"] == 0


def test_proposal_remap_names_old_and_new_ids(
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
    first = _write_csv(
        tmp_path / "a.csv",
        _round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
    )
    build_day(tva_root, date(2026, 9, 14), executions=first, venue="amp")
    store.write_json(
        store.proposals_path(tva_root, a.id),
        {
            "schema_version": "1",
            "session_id": a.id,
            "proposals": [
                {
                    "tva_trade_id": "T01",
                    "proposed_tags": ["OR"],
                    "source_segs": ["s1"],
                    "status": "confirmed",
                }
            ],
            "gaps": [],
        },
    )
    second = _write_csv(
        tmp_path / "b.csv",
        [
            *_round_turn("2026-09-14T06:30:00+0000", "2026-09-14T06:31:00+0000", spread="out"),
            *_round_turn("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="g1"),
        ],
    )
    build_day(tva_root, date(2026, 9, 14), executions=second, venue="amp")
    payload = load_current_day_json(tva_root, date(2026, 9, 14))
    assert payload is not None
    assert payload["proposal_remaps"]
    assert any(
        item["old_tva_trade_id"] == "T01" and item["new_tva_trade_id"] != "T01"
        for item in payload["proposal_remaps"]
    )
    remapped = store.read_json(store.proposals_path(tva_root, a.id))
    assert remapped["proposals"][0]["tva_trade_id"] != "T01"
    assert remapped["proposals"][0]["status"] == "confirmed"
    assert store.tradesviz_tags_path(tva_root, a.id).is_file()


def test_corrupt_legacy_neighbor_parquet_fails_closed(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DAY_MANIFEST, "1")
    legacy = _session(
        tva_root,
        "2026-09-14_233000",
        start=datetime(2026, 9, 14, 23, 30, tzinfo=VIENNA),
        duration_s=3600.0,
    )
    store.fills_path(tva_root, legacy.id).write_text("not-parquet", encoding="utf-8")
    _four_on(monkeypatch)
    _session(
        tva_root,
        "2026-09-15_090000",
        start=datetime(2026, 9, 15, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="c" * 64,
    )
    _session(
        tva_root,
        "2026-09-15_110000",
        start=datetime(2026, 9, 15, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="d" * 64,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _round_turn("2026-09-14T22:45:00+0000", "2026-09-14T22:46:00+0000", spread="leg"),
    )
    with pytest.raises(DayManifestError, match="unlesbar"):
        build_day(tva_root, date(2026, 9, 15), executions=csv, venue="amp")
