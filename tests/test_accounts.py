from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradevidanalyser.accounts import (
    TOPSTEP_DESK,
    TOPSTEP_ON_OTHER_ERROR,
    AccountsError,
    assert_desk_allowed,
    default_accounts_path,
    desk_rules_for_account,
    desk_rules_for_trades,
    load_accounts_config,
)
from tradevidanalyser.rules import RuleTrade, RulesConfig

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


def test_shipped_accounts_yaml_is_synthetic() -> None:
    text = default_accounts_path().read_text(encoding="utf-8")
    assert "synth-amp" in text
    assert "synth-topstepx" in text
    cfg = load_accounts_config()
    assert cfg.accounts["synth-topstepx"].desk == TOPSTEP_DESK
    assert cfg.accounts["synth-amp"].desk == "amp"


def test_topstep_desk_not_applied_to_amp() -> None:
    amp = desk_rules_for_account("synth-amp")
    topstep = desk_rules_for_account("synth-topstepx")
    assert amp.max_trades_per_day == 20
    assert topstep.max_trades_per_day == 10
    with pytest.raises(AccountsError, match=TOPSTEP_ON_OTHER_ERROR):
        assert_desk_allowed("synth-amp", TOPSTEP_DESK)


def test_desk_rules_for_amp_trades() -> None:
    cfg = desk_rules_for_trades([_trade(account="synth-amp")])
    assert cfg.max_trades_per_day == 20
    assert isinstance(cfg, RulesConfig)


def test_unknown_account_is_loud() -> None:
    with pytest.raises(AccountsError, match="Konto nicht in accounts.yaml"):
        desk_rules_for_account("live-topstep-uuid")
