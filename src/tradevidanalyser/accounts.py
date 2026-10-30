"""Per-account desk rules from ``accounts.yaml`` (TT:903). Synthetic example only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tradevidanalyser.rules import RulesConfig, _config_from_mapping

TOPSTEP_DESK = "topstep"
ACCOUNT_MISSING = "Konto fehlt"
ACCOUNT_UNKNOWN = "Konto nicht in accounts.yaml"
MULTI_ACCOUNT_ERROR = "mehrere Konten"
TOPSTEP_ON_OTHER_ERROR = "Topstep-Regeln gelten nicht für andere Konten"


class AccountsError(ValueError):
    """Unknown account, missing account, or Topstep desk applied elsewhere."""


@dataclass(frozen=True)
class AccountEntry:
    account: str
    desk: str
    venue: str | None = None


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


def load_accounts_config(path: Path | None = None) -> AccountsConfig:
    target = path or default_accounts_path()
    raw = yaml.safe_load(target.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AccountsError("accounts.yaml must be a mapping")
    accounts_raw = raw.get("accounts")
    desks_raw = raw.get("desks")
    if not isinstance(accounts_raw, dict) or not accounts_raw:
        raise AccountsError("accounts.yaml missing accounts")
    if not isinstance(desks_raw, dict) or not desks_raw:
        raise AccountsError("accounts.yaml missing desks")
    desks: dict[str, RulesConfig] = {}
    for name, payload in desks_raw.items():
        if not isinstance(payload, dict):
            raise AccountsError(f"desk {name!r} must be a mapping")
        desks[str(name)] = _config_from_mapping(payload)
    accounts: dict[str, AccountEntry] = {}
    for account_id, payload in accounts_raw.items():
        if not isinstance(payload, dict):
            raise AccountsError(f"account {account_id!r} must be a mapping")
        desk = str(payload.get("desk") or "").strip()
        if not desk:
            raise AccountsError(f"account {account_id!r} missing desk")
        if desk not in desks:
            raise AccountsError(f"account {account_id!r} desk {desk!r} is not in desks")
        venue = payload.get("venue")
        accounts[str(account_id)] = AccountEntry(
            account=str(account_id),
            desk=desk,
            venue=None if venue is None else str(venue),
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
    return cfg.desks[entry.desk]


def assert_desk_allowed(account: str | None, desk: str, *, config: AccountsConfig | None = None) -> None:
    if desk != TOPSTEP_DESK:
        return
    entry = account_entry(account, config=config)
    if entry.desk != TOPSTEP_DESK:
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
        assert_desk_allowed(str(raw).strip(), cfg.accounts[account].desk, config=cfg)
    return desk_rules_for_account(account, config=cfg)
