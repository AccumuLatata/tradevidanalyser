from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradevidanalyser.accounts import (
    ACCOUNT_MISSING,
    ENV_ACCOUNTS_FILE,
    ENV_DESK_RULES_FILE,
    TOPSTEP_DESK,
    TOPSTEP_ON_OTHER_ERROR,
    AccountsError,
    assert_desk_allowed,
    default_accounts_path,
    default_desk_rules_path,
    desk_rules_for_account,
    desk_rules_for_trades,
    load_accounts_config,
)
from tradevidanalyser.flags import ENV_NET_FEES
from tradevidanalyser.rules import RuleTrade, RulesConfig, build_rules_report
from tradevidanalyser.schema import RecordingInfo, SessionRecord
from tradevidanalyser import store

T0 = datetime(2026, 9, 11, 14, 30, tzinfo=timezone.utc)


def _trade(*, account: str | None, n: int = 1) -> RuleTrade:
    return RuleTrade(
        tva_trade_id=f"T{n:02d}",
        instrument="MNQ",
        direction="long",
        entry_timestamp=T0,
        exit_timestamp=T0,
        entry_price=21000.0,
        exit_price=21001.0,
        stop_price=None,
        hold_seconds=60.0,
        net_pnl_currency=1.0,
        gross_pnl_currency=1.0,
        status="closed",
        account=account,
    )


def test_fee_schedule_must_be_per_instrument_mapping(tmp_path) -> None:
    import yaml

    raw = yaml.safe_load(default_accounts_path().read_text(encoding="utf-8"))
    raw["accounts"][0]["fee_schedule"] = "topstepx"
    bad = tmp_path / "accounts.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(AccountsError, match="per-instrument"):
        load_accounts_config(bad)


def test_shipped_accounts_yaml_is_tt_list() -> None:
    text = default_accounts_path().read_text(encoding="utf-8")
    assert "synth-amp" in text
    assert "synth-topstepx" in text
    assert "fee_schedule" in text
    assert "trading_days" in text
    cfg = load_accounts_config()
    assert isinstance(cfg.accounts["synth-topstepx"].trading_days, tuple)
    assert isinstance(cfg.accounts["synth-topstepx"].fee_schedule, dict)
    assert "MNQ" in cfg.accounts["synth-topstepx"].fee_schedule
    assert cfg.accounts["synth-topstepx"].desk == "topstepx"
    assert cfg.accounts["synth-amp"].desk == "amp"


def test_topstep_desk_not_applied_to_amp() -> None:
    amp = desk_rules_for_account("synth-amp")
    topstep = desk_rules_for_account("synth-topstepx")
    assert amp.max_trades_per_day == 20
    assert topstep.max_trades_per_day == 10
    with pytest.raises(AccountsError, match=TOPSTEP_ON_OTHER_ERROR):
        assert_desk_allowed("synth-amp", TOPSTEP_DESK)


def test_topstep_desk_override_on_amp_trades_is_loud() -> None:
    """Fails if assert_desk_allowed is compared against the account's own desk."""
    cfg = load_accounts_config()
    trades = [_trade(account="synth-amp")]
    assert desk_rules_for_trades(trades, config=cfg).max_trades_per_day == 20
    with pytest.raises(AccountsError, match=TOPSTEP_ON_OTHER_ERROR):
        desk_rules_for_trades(trades, config=cfg, desk=TOPSTEP_DESK)


def test_desk_rules_for_amp_trades() -> None:
    cfg = desk_rules_for_trades([_trade(account="synth-amp")])
    assert cfg.max_trades_per_day == 20
    assert isinstance(cfg, RulesConfig)


def test_unknown_account_is_loud() -> None:
    with pytest.raises(AccountsError, match="Konto nicht in accounts.yaml"):
        desk_rules_for_account("live-topstep-id")


def test_missing_account_under_net_fees_is_loud(
    tva_root, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_NET_FEES, "1")
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
    from tradevidanalyser.fills import trades_table

    table = trades_table([], venue="amp")
    store.trades_path(tva_root, record.id).parent.mkdir(parents=True, exist_ok=True)
    from tradevidanalyser.fills import write_table

    write_table(store.trades_path(tva_root, record.id), table)
    with pytest.raises(AccountsError, match=ACCOUNT_MISSING):
        build_rules_report(record, root=tva_root)


def test_trade_without_account_does_not_fall_back_to_rules_yaml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_NET_FEES, "1")
    with pytest.raises(AccountsError, match=ACCOUNT_MISSING):
        desk_rules_for_trades([_trade(account=None)])


def test_accounts_file_override_uses_local_yaml(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    shipped = yaml.safe_load(default_accounts_path().read_text(encoding="utf-8"))
    local = tmp_path / "accounts.yaml"
    local.write_text(yaml.safe_dump(shipped, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(ENV_ACCOUNTS_FILE, str(local))
    assert default_accounts_path() == local
    cfg = load_accounts_config()
    assert "synth-amp" in cfg.accounts


def test_desk_rules_file_override_uses_local_yaml(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    shipped = yaml.safe_load(default_desk_rules_path().read_text(encoding="utf-8"))
    shipped["desks"]["amp"]["max_trades_per_day"] = 99
    local = tmp_path / "desk_rules.yaml"
    local.write_text(yaml.safe_dump(shipped, sort_keys=False), encoding="utf-8")
    monkeypatch.setenv(ENV_DESK_RULES_FILE, str(local))
    assert default_desk_rules_path() == local
    cfg = load_accounts_config()
    assert cfg.desks["amp"].max_trades_per_day == 99


def test_desk_rules_file_unset_uses_shipped(monkeypatch: pytest.MonkeyPatch) -> None:
    from tradevidanalyser import accounts as accounts_mod

    monkeypatch.delenv(ENV_DESK_RULES_FILE, raising=False)
    path = default_desk_rules_path()
    assert path.is_file()
    assert path == Path(accounts_mod.__file__).resolve().parent / "desk_rules.yaml"
    cfg = load_accounts_config()
    assert cfg.desks["amp"].max_trades_per_day == 20


def test_desk_rules_file_missing_is_loud(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "no-such-desk-rules.yaml"
    monkeypatch.setenv(ENV_DESK_RULES_FILE, str(missing))
    with pytest.raises(FileNotFoundError, match="desk_rules.yaml not found"):
        default_desk_rules_path()
