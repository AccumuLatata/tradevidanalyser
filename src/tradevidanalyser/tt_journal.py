"""Direct imports of ThesisTester public journal symbols. No name guessing."""

from __future__ import annotations

import importlib
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


def write_journal_positions(*args: Any, **kwargs: Any) -> Any:
    fn = require_tt_symbol(POSITIONS_MODULE, "write_journal_positions")
    return fn(*args, **kwargs)


def ingest_topstepx(*args: Any, **kwargs: Any) -> Any:
    fn = require_tt_symbol(TOPSTEPX_MODULE, "ingest_topstepx")
    return fn(*args, **kwargs)


def write_topstepx_artifacts(*args: Any, **kwargs: Any) -> Any:
    fn = require_tt_symbol(TOPSTEPX_MODULE, "write_topstepx_artifacts")
    return fn(*args, **kwargs)


def reconcile_files(*args: Any, **kwargs: Any) -> Any:
    fn = require_tt_symbol(RECONCILE_MODULE, "reconcile_files")
    return fn(*args, **kwargs)


def tt_lock_api_available() -> bool:
    try:
        require_tt_symbol(LOCK_MODULE, "validate_week_manifest")
        require_tt_symbol(LOCK_MODULE, "assert_manifest_inputs_match")
        require_tt_symbol(LOCK_MODULE, "assert_claimed_lock_supported")
    except ImportError:
        return False
    return True


def tt_position_api_available() -> bool:
    try:
        require_tt_symbol(POSITIONS_MODULE, "build_journal_positions")
    except ImportError:
        return False
    return True


def hashed_input_files(manifest: dict[str, Any], journal_dir: Path) -> dict[str, Path]:
    """Resolve the manifest's hashed input files against ``journal_dir``."""
    raw = manifest.get("files")
    if raw is None:
        raise ValueError("Wochenmanifest hashed inputs fehlen")
    files: dict[str, Path] = {}
    if isinstance(raw, dict):
        for name, spec in raw.items():
            key = str(name)
            if isinstance(spec, dict) and spec.get("path"):
                path = Path(str(spec["path"]))
                files[key] = path if path.is_absolute() else journal_dir / path
            else:
                files[key] = journal_dir / key
        return files
    if isinstance(raw, list):
        for spec in raw:
            if isinstance(spec, str):
                files[spec] = journal_dir / spec
            elif isinstance(spec, dict):
                name = spec.get("name") or spec.get("path")
                if not name:
                    raise ValueError("Wochenmanifest hashed inputs fehlen")
                path = Path(str(spec.get("path") or name))
                files[str(name)] = path if path.is_absolute() else journal_dir / path
            else:
                raise ValueError("Wochenmanifest hashed inputs fehlen")
        return files
    raise ValueError("Wochenmanifest hashed inputs fehlen")


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
