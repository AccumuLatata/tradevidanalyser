"""TT ``accounts.yaml`` plus TVA desk-rule map (TT:903). Synthetic example only."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

from tradevidanalyser.rules import RulesConfig, _config_from_mapping

ENV_ACCOUNTS_FILE = "TVA_ACCOUNTS_FILE"

TOPSTEP_DESK = "topstepx"
ACCOUNT_MISSING = "Konto fehlt"
ACCOUNT_UNKNOWN = "Konto nicht in accounts.yaml"
ACCOUNT_MISMATCH = "Konto stimmt nicht"
MULTI_ACCOUNT_ERROR = "mehrere Konten"
TOPSTEP_ON_OTHER_ERROR = "Topstep-Regeln gelten nicht für andere Konten"
FEE_SCHEDULE_ERROR = "fee_schedule must be a per-instrument mapping"
TRADING_DAYS_ERROR = "trading_days must be an explicit list of dates"


class AccountsError(ValueError):
    """Unknown account, missing account, or Topstep desk applied elsewhere."""


@dataclass(frozen=True)
class AccountEntry:
    account: str
    fee_schedule: dict[str, float | dict[str, float]]
    trading_days: tuple[date, ...]
    desk: str


@dataclass(frozen=True)
class AccountsConfig:
    accounts: dict[str, AccountEntry]
    desks: dict[str, RulesConfig]
    account_desks: dict[str, str]


def default_accounts_path() -> Path:
    override = (os.environ.get(ENV_ACCOUNTS_FILE) or "").strip()
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"accounts.yaml not found: {path}")
        return path
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


def _parse_fee_schedule(raw: Any, account_id: str) -> dict[str, float | dict[str, float]]:
    if not isinstance(raw, dict) or not raw:
        raise AccountsError(f"account {account_id!r}: {FEE_SCHEDULE_ERROR}")
    out: dict[str, float | dict[str, float]] = {}
    for instrument, spec in raw.items():
        key = str(instrument).strip()
        if not key:
            raise AccountsError(f"account {account_id!r}: {FEE_SCHEDULE_ERROR}")
        if isinstance(spec, bool) or not isinstance(spec, (int, float, dict)):
            raise AccountsError(f"account {account_id!r}: {FEE_SCHEDULE_ERROR}")
        if isinstance(spec, (int, float)):
            out[key] = float(spec)
            continue
        if "rt_usd" not in spec:
            raise AccountsError(f"account {account_id!r}: {FEE_SCHEDULE_ERROR}")
        out[key] = {"rt_usd": float(spec["rt_usd"])}
    return out


def _parse_trading_days(raw: Any, account_id: str) -> tuple[date, ...]:
    if not isinstance(raw, list) or not raw:
        raise AccountsError(f"account {account_id!r}: {TRADING_DAYS_ERROR}")
    days: list[date] = []
    for item in raw:
        if isinstance(item, datetime):
            days.append(item.date())
            continue
        if isinstance(item, date):
            days.append(item)
            continue
        try:
            days.append(date.fromisoformat(str(item)))
        except ValueError as exc:
            raise AccountsError(f"account {account_id!r}: {TRADING_DAYS_ERROR}") from exc
    return tuple(days)


def _desk_map(desks_raw: dict[str, Any]) -> dict[str, str]:
    mapping = desks_raw.get("accounts")
    if not isinstance(mapping, dict) or not mapping:
        raise AccountsError("desk_rules.yaml missing account → desk map")
    out: dict[str, str] = {}
    for account_id, desk in mapping.items():
        key = str(account_id).strip()
        name = str(desk).strip()
        if not key or not name:
            raise AccountsError("desk_rules.yaml missing account → desk map")
        out[key] = name
    return out


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
    account_desks = _desk_map(desks_raw)
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
        fee_schedule = _parse_fee_schedule(payload.get("fee_schedule"), account_id)
        trading_days = _parse_trading_days(payload.get("trading_days"), account_id)
        desk = account_desks.get(account_id)
        if desk is None:
            raise AccountsError(f"account {account_id!r} has no desk in desk_rules.yaml")
        if desk not in desks:
            raise AccountsError(f"account {account_id!r} desk {desk!r} is not in desk_rules.yaml")
        accounts[account_id] = AccountEntry(
            account=account_id,
            fee_schedule=fee_schedule,
            trading_days=trading_days,
            desk=desk,
        )
    return AccountsConfig(accounts=accounts, desks=desks, account_desks=account_desks)


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


def assert_desk_allowed(
    account: str | None, desk: str, *, config: AccountsConfig | None = None
) -> None:
    """Topstep desk/rules must not be applied to a non-Topstep account."""
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
