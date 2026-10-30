"""TT ``accounts.yaml`` plus TVA desk-rule map (TT:903). Synthetic example only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tradevidanalyser.rules import RulesConfig, _config_from_mapping

TOPSTEP_DESK = "topstepx"
TOPSTEP_SCHEDULES = frozenset({"topstepx", "topstep"})
ACCOUNT_MISSING = "Konto fehlt"
ACCOUNT_UNKNOWN = "Konto nicht in accounts.yaml"
MULTI_ACCOUNT_ERROR = "mehrere Konten"
TOPSTEP_ON_OTHER_ERROR = "Topstep-Regeln gelten nicht für andere Konten"


class AccountsError(ValueError):
    """Unknown account, missing account, or Topstep desk applied elsewhere."""


@dataclass(frozen=True)
class AccountEntry:
    account: str
    fee_schedule: str
    trading_days: tuple[str, ...]
    desk: str


@dataclass(frozen=True)
class AccountsConfig:
    accounts: dict[str, AccountEntry]
    desks: dict[str, RulesConfig]


def default_accounts_path() -> Path:
    here = Path(__file__).resolve().parent
    path = here / "accounts.yaml"
    if not path.is_file():
        raise FileNotFoundError("accounts.yaml not found (keep the shipped synthetic file)")
    return path


def default_desk_rules_path() -> Path:
    here = Path(__file__).resolve().parent
    path = here / "desk_rules.yaml"
    if not path.is_file():
        raise FileNotFoundError("desk_rules.yaml not found (keep the shipped TVA desk map)")
    return path


def _desk_name_for_schedule(fee_schedule: str) -> str:
    if fee_schedule in TOPSTEP_SCHEDULES:
        return TOPSTEP_DESK
    return fee_schedule


def _is_topstep_account(entry: AccountEntry) -> bool:
    return entry.fee_schedule in TOPSTEP_SCHEDULES or entry.desk == TOPSTEP_DESK


def load_accounts_config(
    path: Path | None = None,
    *,
    desk_path: Path | None = None,
) -> AccountsConfig:
    target = path or default_accounts_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AccountsError("accounts.yaml must be a mapping")
    accounts_raw = raw.get("accounts")
    if not isinstance(accounts_raw, list) or not accounts_raw:
        raise AccountsError("accounts.yaml missing accounts list")
    desks_raw = yaml.safe_load((desk_path or default_desk_rules_path()).read_text(encoding="utf-8"))
    if not isinstance(desks_raw, dict) or not isinstance(desks_raw.get("desks"), dict):
        raise AccountsError("desk_rules.yaml missing desks")
    desks: dict[str, RulesConfig] = {}
    for name, payload in desks_raw["desks"].items():
        if not isinstance(payload, dict):
            raise AccountsError(f"desk {name!r} must be a mapping")
        desks[str(name)] = _config_from_mapping(payload)
    accounts: dict[str, AccountEntry] = {}
    for index, payload in enumerate(accounts_raw):
        if not isinstance(payload, dict):
            raise AccountsError(f"accounts[{index}] must be a mapping")
        account_id = str(payload.get("account") or "").strip()
        if not account_id:
            raise AccountsError(f"accounts[{index}] missing account")
        fee_schedule = str(payload.get("fee_schedule") or "").strip()
        if not fee_schedule:
            raise AccountsError(f"account {account_id!r} missing fee_schedule")
        days_raw = payload.get("trading_days")
        if not isinstance(days_raw, list) or not days_raw:
            raise AccountsError(f"account {account_id!r} missing trading_days")
        desk = _desk_name_for_schedule(fee_schedule)
        if desk not in desks:
            raise AccountsError(f"account {account_id!r} fee_schedule {fee_schedule!r} has no desk")
        accounts[account_id] = AccountEntry(
            account=account_id,
            fee_schedule=fee_schedule,
            trading_days=tuple(str(day) for day in days_raw),
            desk=desk,
        )
    return AccountsConfig(accounts=accounts, desks=desks)


def account_entry(account: str | None, *, config: AccountsConfig | None = None) -> AccountEntry:
    if account is None or not str(account).strip():
        raise AccountsError(ACCOUNT_MISSING)
    cfg = config or load_accounts_config()
    key = str(account).strip()
    entry = cfg.accounts.get(key)
    if entry is None:
        raise AccountsError(f"{ACCOUNT_UNKNOWN}: {key}")
    return entry


def desk_rules_for_account(
    account: str | None,
    *,
    config: AccountsConfig | None = None,
) -> RulesConfig:
    cfg = config or load_accounts_config()
    entry = account_entry(account, config=cfg)
    assert_desk_allowed(account, entry.desk, config=cfg)
    return cfg.desks[entry.desk]


def assert_desk_allowed(account: str | None, desk: str, *, config: AccountsConfig | None = None) -> None:
    """Topstep desk/rules must not be applied to a non-Topstep account."""
    if desk not in TOPSTEP_SCHEDULES and desk != TOPSTEP_DESK:
        return
    entry = account_entry(account, config=config)
    if not _is_topstep_account(entry):
        raise AccountsError(TOPSTEP_ON_OTHER_ERROR)


def accounts_from_trades(trades: list[Any]) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for trade in trades:
        raw = getattr(trade, "account", None)
        if raw is None or not str(raw).strip():
            continue
        key = str(raw).strip()
        if key not in seen:
            seen.add(key)
            found.append(key)
    return found


def desk_rules_for_trades(
    trades: list[Any],
    *,
    config: AccountsConfig | None = None,
    desk: str | None = None,
) -> RulesConfig:
    cfg = config or load_accounts_config()
    accounts = accounts_from_trades(trades)
    if not accounts:
        raise AccountsError(ACCOUNT_MISSING)
    if len(accounts) > 1:
        raise AccountsError(MULTI_ACCOUNT_ERROR)
    account = accounts[0]
    for trade in trades:
        raw = getattr(trade, "account", None)
        if raw is None or not str(raw).strip():
            raise AccountsError(ACCOUNT_MISSING)
        if str(raw).strip() != account:
            raise AccountsError(MULTI_ACCOUNT_ERROR)
    chosen = desk if desk is not None else account_entry(account, config=cfg).desk
    assert_desk_allowed(account, chosen, config=cfg)
    if chosen not in cfg.desks:
        raise AccountsError(f"desk {chosen!r} is not in desk_rules.yaml")
    return cfg.desks[chosen]
