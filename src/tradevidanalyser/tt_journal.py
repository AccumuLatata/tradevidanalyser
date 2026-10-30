"""Direct imports of ThesisTester public journal symbols. No name guessing."""

from __future__ import annotations

import importlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

LOCK_MODULE = "thesistester.journal.lock"
POSITIONS_MODULE = "thesistester.journal.positions"
TT_LOCK_VALIDATE_MISSING = f"{LOCK_MODULE}.validate_week_manifest fehlt"
TOPSTEPX_MODULE = "thesistester.journal.topstepx_reconcile"
RECONCILE_MODULE = "thesistester.journal.reconcile"

JOURNAL_TRADES_NAME = "journal_trades.parquet"
JOURNAL_LOT_SOURCES_NAME = "journal_lot_sources.parquet"
JOURNAL_POSITIONS_PARQUET = "journal_positions.parquet"
JOURNAL_POSITIONS_JSON = "positions.json"
WEEK_MANIFEST_NAME = "week_manifest.json"


def require_tt_symbol(module: str, name: str) -> Any:
    """Import ``module.name`` or raise naming the missing symbol."""
    try:
        mod = importlib.import_module(module)
    except ImportError as exc:
        raise ImportError(f"{module}.{name} fehlt") from exc
    obj = getattr(mod, name, None)
    if obj is None:
        raise ImportError(f"{module}.{name} fehlt")
    return obj


def validate_week_manifest(manifest: Any) -> Any:
    fn = require_tt_symbol(LOCK_MODULE, "validate_week_manifest")
    return fn(manifest)


def assert_manifest_inputs_match(manifest: Any, files: Any) -> Any:
    fn = require_tt_symbol(LOCK_MODULE, "assert_manifest_inputs_match")
    return fn(manifest, files)


def assert_claimed_lock_supported(manifest: Any, files: Any) -> Any:
    fn = require_tt_symbol(LOCK_MODULE, "assert_claimed_lock_supported")
    return fn(manifest, files=files)


def build_journal_positions(trades: Any, lot_sources: Any) -> Any:
    fn = require_tt_symbol(POSITIONS_MODULE, "build_journal_positions")
    return fn(trades, lot_sources)


def write_journal_positions(
    *,
    journal_dir: Any,
    account: str,
    output_dir: Any,
) -> Any:
    fn = require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    return fn(journal_dir=journal_dir, account=account, output_dir=output_dir)


def ingest_topstepx(
    *,
    trades: Any,
    tradesviz: Any,
    account: str,
    accounts: Any,
    output_dir: Any,
    accept_phantoms: Any = None,
    journal_risk_ticks: Any = None,
) -> Any:
    fn = require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    kwargs: dict[str, Any] = {
        "trades": trades,
        "tradesviz": tradesviz,
        "account": account,
        "accounts": accounts,
        "output_dir": output_dir,
    }
    if accept_phantoms is not None:
        kwargs["accept_phantoms"] = accept_phantoms
    if journal_risk_ticks is not None:
        kwargs["journal_risk_ticks"] = journal_risk_ticks
    return fn(**kwargs)


def reconcile_topstepx(
    lots: Any,
    fills: Any,
    *,
    account: str,
    fee_schedule: Mapping[str, float],
    accepted_phantoms: Any = (),
    journal_risk_ticks: Any = None,
) -> Any:
    fn = require_tt_symbol(TOPSTEPX_MODULE, "reconcile_topstepx")
    kwargs: dict[str, Any] = {
        "account": account,
        "fee_schedule": fee_schedule,
        "accepted_phantoms": accepted_phantoms,
    }
    if journal_risk_ticks is not None:
        kwargs["journal_risk_ticks"] = journal_risk_ticks
    return fn(lots, fills, **kwargs)


def write_topstepx_artifacts(
    output_dir: Any,
    trades: Any,
    sources: Any,
    days: Any,
    *,
    account: str,
) -> Any:
    fn = require_tt_symbol(TOPSTEPX_MODULE, "write_topstepx_artifacts")
    return fn(output_dir, trades, sources, days, account=account)


def lock_files(
    *,
    week: str,
    account: str,
    accounts: Any,
    journal_dir: Any,
    positions_dir: Any,
    prepare_manifest: Any,
    output_dir: Any,
    tva_inventory: Any = None,
    exceptions: Any = None,
) -> Any:
    fn = require_tt_symbol(LOCK_MODULE, "lock_files")
    return fn(
        week=week,
        account=account,
        accounts=accounts,
        journal_dir=journal_dir,
        positions_dir=positions_dir,
        prepare_manifest=prepare_manifest,
        output_dir=output_dir,
        tva_inventory=tva_inventory,
        exceptions=exceptions,
    )


def reconcile_files(*args: Any, **kwargs: Any) -> Any:
    fn = require_tt_symbol(RECONCILE_MODULE, "reconcile_files")
    return fn(*args, **kwargs)


def tt_lock_api_available() -> bool:
    try:
        require_tt_symbol(LOCK_MODULE, "validate_week_manifest")
        require_tt_symbol(LOCK_MODULE, "assert_manifest_inputs_match")
        require_tt_symbol(LOCK_MODULE, "assert_claimed_lock_supported")
        require_tt_symbol(LOCK_MODULE, "lock_files")
    except ImportError:
        return False
    return True


def tt_position_api_available() -> bool:
    try:
        require_tt_symbol(POSITIONS_MODULE, "build_journal_positions")
        require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    except ImportError:
        return False
    return True


def tt_writer_api_available() -> bool:
    try:
        require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
        require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
        require_tt_symbol(LOCK_MODULE, "lock_files")
    except ImportError:
        return False
    return True


WEEK_MANIFEST_INPUTS = "Wochenmanifest hashed inputs fehlen"
WEEK_MANIFEST_ROLE_UNKNOWN = "Wochenmanifest role unbekannt"
WEEK_MANIFEST_INPUT_MISSING = "Wochenmanifest input fehlt"

# TT week_manifest.json inputs[].role → lock_files source the caller supplied.
_INPUT_ROLE_SOURCES = {
    "journal": "journal_dir",
    "journal_dir": "journal_dir",
    "positions": "positions_dir",
    "positions_dir": "positions_dir",
    "prepare": "prepare_manifest",
    "prepare_manifest": "prepare_manifest",
    "accounts": "accounts",
}


def lock_source_paths(
    root: Path,
    *,
    journal_dir: Path | None = None,
    positions_dir: Path | None = None,
    prepare_manifest: Path | None = None,
    accounts: Path | None = None,
) -> dict[str, Path]:
    """Conventional lock sources under ``root``, with explicit overrides."""
    return {
        "journal_dir": Path(journal_dir) if journal_dir is not None else Path(root) / "journal",
        "positions_dir": Path(positions_dir)
        if positions_dir is not None
        else Path(root) / "positions",
        "prepare_manifest": (
            Path(prepare_manifest)
            if prepare_manifest is not None
            else Path(root) / "prepare" / "prepare_manifest.json"
        ),
        "accounts": Path(accounts) if accounts is not None else Path(root) / "accounts.yaml",
    }


def _resolve_role_path(source: Path, basename: str) -> Path:
    name = Path(basename)
    if name.is_absolute():
        return name
    if source.is_file():
        if source.name == name.name:
            return source
        return source.parent / name.name
    return source / name.name


def hashed_input_files(
    manifest: dict[str, Any],
    *,
    journal_dir: Path,
    positions_dir: Path,
    prepare_manifest: Path,
    accounts: Path,
) -> dict[str, Path]:
    """Map TT ``inputs: [{role, path, sha256}]`` to the caller-supplied sources.

    Roles are ``journal`` / ``positions`` / ``prepare`` / ``accounts`` (and the
    lock_files kwarg aliases). Unknown role or a missing path is loud.
    """
    raw = manifest.get("inputs")
    if not isinstance(raw, list) or not raw:
        raise ValueError(WEEK_MANIFEST_INPUTS)
    supplied = {
        "journal_dir": Path(journal_dir),
        "positions_dir": Path(positions_dir),
        "prepare_manifest": Path(prepare_manifest),
        "accounts": Path(accounts),
    }
    files: dict[str, Path] = {}
    for spec in raw:
        if not isinstance(spec, dict):
            raise ValueError(WEEK_MANIFEST_INPUTS)
        role = spec.get("role")
        source_key = _INPUT_ROLE_SOURCES.get(str(role) if role is not None else "")
        if source_key is None:
            raise ValueError(f"{WEEK_MANIFEST_ROLE_UNKNOWN}: {role!r}")
        source = supplied[source_key]
        basename = spec.get("path")
        if basename is None or str(basename).strip() == "":
            raise ValueError(WEEK_MANIFEST_INPUTS)
        resolved = _resolve_role_path(source, str(basename))
        if not resolved.exists():
            raise ValueError(f"{WEEK_MANIFEST_INPUT_MISSING}: {role} {basename}")
        files[str(role)] = source
    return files


def is_tt_journal_dir(path: Path) -> bool:
    root = Path(path)
    return root.is_dir() and (root / JOURNAL_TRADES_NAME).is_file()


def journal_positions_path(journal_dir: Path) -> Path | None:
    parquet = Path(journal_dir) / JOURNAL_POSITIONS_PARQUET
    if parquet.is_file():
        return parquet
    json_path = Path(journal_dir) / JOURNAL_POSITIONS_JSON
    if json_path.is_file():
        return json_path
    return None
