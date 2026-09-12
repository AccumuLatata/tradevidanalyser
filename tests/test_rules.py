from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, fills_session, transcribe_session
from tradevidanalyser.rules import (
    ENV_RULES,
    EVIDENCE_BACKED_RULE_IDS,
    RULE_IDS,
    ReentryConfig,
    RuleTrade,
    RulesConfig,
    default_rules_path,
    evaluate_rules,
    load_rules_config,
    parse_rules_yaml,
    read_rule_trades,
    realized_pnl,
    rules_session,
)
from tradevidanalyser.schema import RecordingInfo, SessionRecord
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app

T0 = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)


def _trade(
    n: int,
    *,
    entry_s: float,
    exit_s: float | None,
    pnl: float | None,
    net: bool = True,
    hold: float | None = 600.0,
    direction: str = "long",
    instrument: str = "MNQ",
    entry_price: float | None = 21000.0,
    exit_price: float | None = None,
    stop_price: float | None = None,
    status: str = "",
) -> RuleTrade:
    exit_ts = None if exit_s is None else T0 + timedelta(seconds=exit_s)
    if exit_price is None and exit_s is not None and pnl is not None:
        exit_price = (entry_price or 21000.0) + (1.0 if pnl >= 0 else -1.0)
    return RuleTrade(
        tva_trade_id=f"T{n:02d}",
        instrument=instrument,
        direction=direction,
        entry_timestamp=T0 + timedelta(seconds=entry_s),
        exit_timestamp=exit_ts,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_price=stop_price,
        hold_seconds=hold,
        net_pnl_currency=pnl if net else None,
        gross_pnl_currency=None if net else pnl,
        status=status or ("closed" if exit_ts is not None else "open"),
    )


def _status(trades: list[RuleTrade], rule: str, config: RulesConfig | None = None) -> str:
    checks = {item.rule: item for item in evaluate_rules(trades, config)}
    return checks[rule].status


def _check(trades: list[RuleTrade], rule: str, config: RulesConfig | None = None):
    return {item.rule: item for item in evaluate_rules(trades, config)}[rule]


def test_shipped_rules_yaml_keeps_d4_null() -> None:
    cfg = load_rules_config(default_rules_path())
    assert cfg.daily_loss_limit_usd is None
    assert cfg.max_trades_per_day == 10
    assert cfg.consecutive_loss_timeout.n == 3
    assert cfg.consecutive_loss_timeout.minutes == 30
    assert cfg.post_loss_block_minutes == 5
    assert cfg.reentry == ReentryConfig(window_s=120, max=1, block_minutes=5)
    assert cfg.flat_by is None


def test_parse_rules_yaml_comments_and_null() -> None:
    cfg = parse_rules_yaml(default_rules_path().read_text(encoding="utf-8"))
    assert cfg.daily_loss_limit_usd is None
    assert cfg.flat_by is None


def test_repo_and_package_rules_yaml_match() -> None:
    package = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "rules.yaml"
    root = Path(__file__).resolve().parents[1] / "rules.yaml"
    assert package.read_text(encoding="utf-8") == root.read_text(encoding="utf-8")


def test_parse_rules_yaml_null_aliases() -> None:
    cfg = parse_rules_yaml("daily_loss_limit_usd: NULL\nflat_by: None\nmax_trades_per_day: 10\n")
    assert cfg.daily_loss_limit_usd is None
    assert cfg.flat_by is None


def test_bad_rules_yaml_is_value_error() -> None:
    with pytest.raises(ValueError, match="max_trades_per_day"):
        parse_rules_yaml("max_trades_per_day: null\n")
    with pytest.raises(ValueError, match="consecutive_loss_timeout.n"):
        parse_rules_yaml("consecutive_loss_timeout:\n  n: 0\n")


def test_tva_rules_missing_file_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_RULES, str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError, match="TVA_RULES"):
        default_rules_path()


@pytest.mark.parametrize(
    "trades, config, expected",
    [
        ([], RulesConfig(), "unverifiable"),
        ([_trade(1, entry_s=0, exit_s=60, pnl=-10)], RulesConfig(), "unverifiable"),
        ([_trade(1, entry_s=0, exit_s=60, pnl=-100)], RulesConfig(daily_loss_limit_usd=100), "pass"),
        ([_trade(1, entry_s=0, exit_s=60, pnl=-100.01)], RulesConfig(daily_loss_limit_usd=100), "violated"),
        ([_trade(1, entry_s=0, exit_s=60, pnl=20)], RulesConfig(daily_loss_limit_usd=100), "pass"),
        (
            [_trade(1, entry_s=0, exit_s=60, pnl=-40), _trade(2, entry_s=120, exit_s=180, pnl=-40)],
            RulesConfig(daily_loss_limit_usd=100),
            "pass",
        ),
    ],
)
def test_r_dll_table(trades, config, expected) -> None:
    assert _status(trades, "R-DLL", config) == expected


def test_r_dll_gross_fees_unknown_note() -> None:
    trades = [_trade(1, entry_s=0, exit_s=60, pnl=-20, net=False)]
    check = _check(trades, "R-DLL", RulesConfig(daily_loss_limit_usd=100))
    assert check.status == "pass"
    assert check.evidence["fees_unknown"] is True
    assert check.reason == "fees_unknown"
    assert realized_pnl(trades[0]) == (-20.0, "fees_unknown")


def test_r_dll_unknown_pnl_unverifiable() -> None:
    trades = [_trade(1, entry_s=0, exit_s=60, pnl=None)]
    check = _check(trades, "R-DLL", RulesConfig(daily_loss_limit_usd=100))
    assert check.status == "unverifiable"
    assert "T01" in check.evidence["unknown_pnl_trades"]


@pytest.mark.parametrize(
    "count, expected",
    [(0, "pass"), (10, "pass"), (11, "violated")],
)
def test_r_max10_boundary(count, expected) -> None:
    trades = [_trade(i + 1, entry_s=i * 120, exit_s=i * 120 + 30, pnl=1.0) for i in range(count)]
    assert _status(trades, "R-MAX10") == expected


def test_r_max10_exactly_10_trade_ids() -> None:
    trades = [_trade(i + 1, entry_s=i * 60, exit_s=i * 60 + 20, pnl=-1.0) for i in range(10)]
    check = _check(trades, "R-MAX10")
    assert check.status == "pass"
    assert check.evidence["count"] == 10


def _three_losses_then(next_entry_after_third_exit: float) -> list[RuleTrade]:
    # exits at 60, 130, 200
    return [
        _trade(1, entry_s=0, exit_s=60, pnl=-10),
        _trade(2, entry_s=70, exit_s=130, pnl=-10),
        _trade(3, entry_s=140, exit_s=200, pnl=-10),
        _trade(4, entry_s=200 + next_entry_after_third_exit, exit_s=200 + next_entry_after_third_exit + 30, pnl=5),
    ]


@pytest.mark.parametrize(
    "trades, expected",
    [
        (_three_losses_then(1800), "pass"),
        (_three_losses_then(1799), "violated"),
        (
            [
                _trade(1, entry_s=0, exit_s=60, pnl=-10),
                _trade(2, entry_s=70, exit_s=130, pnl=-10),
                _trade(3, entry_s=140, exit_s=200, pnl=5),
            ],
            "pass",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=60, pnl=-10),
                _trade(2, entry_s=70, exit_s=130, pnl=-10),
                _trade(3, entry_s=140, exit_s=200, pnl=-10),
            ],
            "pass",
        ),
    ],
)
def test_r_3l30_boundary(trades, expected) -> None:
    assert _status(trades, "R-3L30") == expected


@pytest.mark.parametrize(
    "trades, expected",
    [
        (
            [
                _trade(1, entry_s=0, exit_s=600, pnl=-10, hold=600),
                _trade(2, entry_s=900, exit_s=930, pnl=5, hold=30),
            ],
            "pass",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=600, pnl=-10, hold=600),
                _trade(2, entry_s=899, exit_s=930, pnl=5, hold=30),
            ],
            "violated",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10, stop_price=20999.0, exit_price=20999.0),
                _trade(2, entry_s=40, exit_s=80, pnl=5, hold=40),
            ],
            "pass",
        ),
        ([_trade(1, entry_s=0, exit_s=60, pnl=8)], "pass"),
    ],
)
def test_r_5m_boundary(trades, expected) -> None:
    assert _status(trades, "R-5M") == expected


def test_r_5m_long_hold_stop_hit_is_not_exempt() -> None:
    trades = [
        _trade(
            1,
            entry_s=0,
            exit_s=600,
            pnl=-10,
            hold=600,
            stop_price=20999.0,
            exit_price=20999.0,
        ),
        _trade(2, entry_s=640, exit_s=700, pnl=5, hold=60),
    ]
    assert _status(trades, "R-5M") == "violated"


def test_r_5m_missing_price_is_not_same_level_exempt() -> None:
    trades = [
        _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
        _trade(2, entry_s=40, exit_s=80, pnl=5, hold=40, entry_price=None),
    ]
    assert _status(trades, "R-5M") == "violated"


@pytest.mark.parametrize(
    "trades, expected",
    [
        (
            [
                _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
                _trade(2, entry_s=40, exit_s=80, pnl=-10, hold=10),
            ],
            "pass",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
                _trade(2, entry_s=40, exit_s=80, pnl=-10, hold=10),
                _trade(3, entry_s=100, exit_s=140, pnl=-10, hold=10),
            ],
            "violated",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
                _trade(2, entry_s=40, exit_s=80, pnl=-10, hold=10),
                _trade(3, entry_s=80 + 300, exit_s=80 + 330, pnl=5, hold=30),
            ],
            "pass",
        ),
        (
            [
                _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
                _trade(2, entry_s=40, exit_s=80, pnl=-10, hold=10),
                _trade(3, entry_s=80 + 299, exit_s=80 + 330, pnl=5, hold=30),
            ],
            "violated",
        ),
    ],
)
def test_r_reentry_table(trades, expected) -> None:
    assert _status(trades, "R-REENTRY") == expected


def test_r_reentry_ignores_non_soe_loss_cluster() -> None:
    trades = [
        _trade(1, entry_s=0, exit_s=600, pnl=-10, hold=600),
        _trade(2, entry_s=640, exit_s=680, pnl=-10, hold=40),
        _trade(3, entry_s=700, exit_s=740, pnl=-10, hold=40),
    ]
    assert _status(trades, "R-REENTRY") == "pass"


def test_r_reentry_same_level_through_other_instrument() -> None:
    trades = [
        _trade(1, entry_s=0, exit_s=10, pnl=-10, hold=10),
        _trade(2, entry_s=20, exit_s=40, pnl=5, hold=20, instrument="MES", entry_price=5000.0),
        _trade(3, entry_s=50, exit_s=80, pnl=-10, hold=30),
        _trade(4, entry_s=100, exit_s=140, pnl=-10, hold=40),
    ]
    assert _status(trades, "R-REENTRY") == "violated"


def test_r_3l30_unknown_pnl_does_not_stitch_losses() -> None:
    trades = [
        _trade(1, entry_s=0, exit_s=60, pnl=-10),
        _trade(2, entry_s=70, exit_s=130, pnl=None),
        _trade(3, entry_s=140, exit_s=200, pnl=-10),
        _trade(4, entry_s=210, exit_s=250, pnl=-10),
        _trade(5, entry_s=260, exit_s=300, pnl=5),
    ]
    assert _status(trades, "R-3L30") == "pass"


def test_r_close_unset_is_unverifiable() -> None:
    trades = [_trade(1, entry_s=0, exit_s=60, pnl=5)]
    check = _check(trades, "R-CLOSE")
    assert check.status == "unverifiable"
    assert check.reason == "flat_by is unset"


def test_r_close_before_and_after_cutoff() -> None:
    cfg = RulesConfig(flat_by=time(16, 0))
    # 14:30 UTC = 10:30 ET; exit 60s later still before 16:00 ET
    assert _status([_trade(1, entry_s=0, exit_s=60, pnl=5)], "R-CLOSE", cfg) == "pass"
    # 21:00 UTC = 17:00 ET on 2026-09-11
    late = _trade(1, entry_s=6 * 3600 + 1800, exit_s=6 * 3600 + 1860, pnl=5)
    assert _status([late], "R-CLOSE", cfg) == "violated"
    opened = _trade(1, entry_s=0, exit_s=None, pnl=None, hold=None)
    assert _status([opened], "R-CLOSE", cfg) == "violated"


def test_catalog_order_and_no_model() -> None:
    checks = evaluate_rules([])
    assert [item.rule for item in checks] == list(RULE_IDS)
    src = Path(__file__).resolve().parents[1] / "src" / "tradevidanalyser" / "rules.py"
    text = src.read_text(encoding="utf-8")
    assert "providers" not in text
    assert "extract" not in text
    backed = [item for item in checks if item.rule in EVIDENCE_BACKED_RULE_IDS]
    assert [item.rule for item in backed] == list(EVIDENCE_BACKED_RULE_IDS)
    assert all(item.status == "unverifiable" for item in backed)


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


def test_cli_rules_writes_facts_and_omits_until_run(tva_root: Path, capsys) -> None:
    record = _session(tva_root)
    assert "rules" not in store.compute_status(tva_root, record.id).stages
    _write_trades(tva_root, record.id, [_trade(1, entry_s=0, exit_s=60, pnl=-10)])
    assert main(["--root", str(tva_root), "rules", record.id]) == 0
    out = capsys.readouterr().out
    assert '"status": "ok"' in out
    report = store.read_json(store.rules_path(tva_root, record.id))
    by_id = {row["rule"]: row for row in report["rules"]}
    assert by_id["R-DLL"]["status"] == "unverifiable"
    assert by_id["R-MAX10"]["status"] == "pass"
    assert by_id["R-CLOSE"]["status"] == "unverifiable"
    assert by_id["R-PLAYBOOK"]["status"] == "unverifiable"
    assert by_id["R-SLTP"]["status"] == "unverifiable"
    assert set(by_id) >= set(RULE_IDS)
    assert store.compute_status(tva_root, record.id).stages["rules"] == "ok"
    first = store.rules_path(tva_root, record.id).read_text(encoding="utf-8")
    assert main(["--root", str(tva_root), "rules", record.id]) == 0
    assert store.rules_path(tva_root, record.id).read_text(encoding="utf-8") == first


def test_no_trades_parquet_skips_and_omits(tva_root: Path) -> None:
    record = _session(tva_root)
    result = rules_session(record.id, root=tva_root)
    assert result.status == "skipped"
    assert not store.rules_path(tva_root, record.id).is_file()
    assert "rules" not in store.compute_status(tva_root, record.id).stages


def test_rules_omitted_from_status_filter(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    assert "rules" not in store.compute_status(tva_root, record.id).stages
    client = TestClient(create_app(tva_root))
    missing = client.get("/sessions", params={"status": "missing"}).json()["sessions"]
    assert record.id not in missing


def test_stale_rules_failed_is_omitted(tva_root: Path, sample_video: Path) -> None:
    record = ingest(sample_video, root=tva_root)
    transcribe_session(record.id, root=tva_root)
    extract_session(record.id, root=tva_root)
    store.compute_status(tva_root, record.id, failed="rules", error="rules exploded")
    status = store.compute_status(tva_root, record.id)
    assert "rules" not in status.stages
    assert status.error is None


def test_rules_refuses_path_escape(tva_root: Path) -> None:
    assert main(["--root", str(tva_root), "rules", "../outside"]) == 1
    assert main(["--root", str(tva_root), "rules", "foo/bar"]) == 1


def test_rules_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "rules" not in ALLOWED_RUN_STAGES


def test_invalidate_downstream_drops_rules(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, [_trade(1, entry_s=0, exit_s=60, pnl=5)])
    rules_session(record.id, root=tva_root)
    assert store.rules_path(tva_root, record.id).is_file()
    store.invalidate_downstream(tva_root, record.id)
    assert not store.rules_path(tva_root, record.id).is_file()
    assert "rules" not in store.compute_status(tva_root, record.id).stages


def test_naive_parquet_timestamps_are_utc(tmp_path: Path) -> None:
    trade = _trade(1, entry_s=0, exit_s=60, pnl=5)
    table = pa.table(
        {
            "tva_trade_id": [trade.tva_trade_id],
            "instrument": [trade.instrument],
            "direction": [trade.direction],
            "entry_timestamp": pa.array(
                [trade.entry_timestamp.replace(tzinfo=None)], type=pa.timestamp("us")
            ),
            "exit_timestamp": pa.array(
                [trade.exit_timestamp.replace(tzinfo=None) if trade.exit_timestamp else None],
                type=pa.timestamp("us"),
            ),
            "entry_price": [trade.entry_price],
            "exit_price": [trade.exit_price],
            "stop_price": [trade.stop_price],
            "hold_seconds": [trade.hold_seconds],
            "net_pnl_currency": [trade.net_pnl_currency],
            "gross_pnl_currency": [trade.gross_pnl_currency],
            "status": [trade.status],
        }
    )
    path = tmp_path / "trades.parquet"
    pq.write_table(table, path)
    rows = read_rule_trades(path)
    assert rows[0].entry_timestamp.tzinfo is not None
    assert _status(rows, "R-CLOSE", RulesConfig(flat_by=time(16, 0))) == "pass"


def test_fills_skip_drops_stale_rules(tva_root: Path) -> None:
    record = _session(tva_root)
    _write_trades(tva_root, record.id, [_trade(1, entry_s=0, exit_s=60, pnl=-10)])
    rules_session(record.id, root=tva_root)
    assert store.rules_path(tva_root, record.id).is_file()
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "skipped"
    assert not store.rules_path(tva_root, record.id).is_file()
    assert "rules" not in store.compute_status(tva_root, record.id).stages


def test_fills_rewrite_drops_stale_rules(tva_root: Path) -> None:
    record = SessionRecord(
        id="2026-05-14_160000",
        recording=RecordingInfo(
            path="recordings/2026-05-14 16-00-00.mp4",
            sha256="0" * 64,
            start_wallclock_vienna="2026-05-14T16:00:00+02:00",
            duration_s=3600.0,
            filename="2026-05-14 16-00-00.mp4",
        ),
    )
    store.save_session(tva_root, record)
    store.write_json(
        store.rules_path(tva_root, record.id),
        {"schema_version": "1", "session_id": record.id, "rules": []},
    )
    csv = Path(__file__).parent / "fixtures" / "tradesviz_synthetic.csv"
    result = fills_session(record.id, root=tva_root, executions=csv, venue="amp")
    assert result.status == "ok"
    assert not store.rules_path(tva_root, record.id).is_file()
    assert "rules" not in store.compute_status(tva_root, record.id).stages
