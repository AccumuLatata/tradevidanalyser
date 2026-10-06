"""L0 snapshot compare (plan §3.1). Used by later PRs; PR-28 only lays the files down."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

L0_ROOT = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_OCR = L0_ROOT / "l0-ocr"
L0_FILENAME = L0_ROOT / "l0-filename"
L0_VARIANTS = ("l0-ocr", "l0-filename")
L0_SESSION_FILES = (
    "session.json",
    "fills.parquet",
    "trades.parquet",
    "evidence.json",
    "rules.json",
    "debrief.md",
    "debrief.json",
    "ledger.json",
    "notion_payload.json",
)
# Strip exactly these fields, nothing else (plan §3.1).
L0_JSON_DROP = frozenset({"app_version", "log_line", "created"})


def l0_variant_dir(name: str) -> Path:
    if name not in L0_VARIANTS:
        raise ValueError(f"unknown L0 variant {name!r}")
    return L0_ROOT / name


def drop_l0_fields(payload: Any) -> Any:
    if isinstance(payload, dict):
        return {
            key: drop_l0_fields(value)
            for key, value in payload.items()
            if key not in L0_JSON_DROP
        }
    if isinstance(payload, list):
        return [drop_l0_fields(item) for item in payload]
    return payload


def canonical_json(payload: Any) -> str:
    return json.dumps(
        drop_l0_fields(payload),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parquet_tables_equal(left: Path, right: Path) -> None:
    a = pq.read_table(left)
    b = pq.read_table(right)
    if a.column_names != b.column_names:
        raise AssertionError(f"parquet columns differ: {a.column_names} vs {b.column_names}")
    a_types = [str(field.type) for field in a.schema]
    b_types = [str(field.type) for field in b.schema]
    if a_types != b_types:
        raise AssertionError(f"parquet types differ: {a_types} vs {b_types}")
    if not a.equals(b, check_metadata=False):
        raise AssertionError(f"parquet cells differ: {left} vs {right}")
