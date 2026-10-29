"""Copy OBS sidecar JSONL next to a recording. Destination is always under TVA_ROOT.

PR-39 only: copy + classify. No facts, no time_anchor.json (PR-40).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tradevidanalyser import config

SIDECAR_OK = "ok"
SIDECAR_MISSING = "missing"
SIDECAR_INVALID_JSON = "invalid_json"
SIDECAR_STATUSES = (SIDECAR_OK, SIDECAR_MISSING, SIDECAR_INVALID_JSON)
EVENTS_JSONL_SUFFIX = ".events.jsonl"


def sidecar_source_path(video: Path) -> Path:
    """``<stem>.jsonl`` beside the video. Never ``<stem>.events.jsonl`` (TT:460, 901)."""
    return video.with_suffix(".jsonl")


def sidecar_dest_path(root: Path, video: Path) -> Path:
    """Sidecar dest is ``TVA_ROOT/recordings/<stem>.jsonl`` — never a hardcoded NAS path."""
    return config.recordings_dir(root) / f"{video.stem}.jsonl"


def classify_jsonl(path: Path) -> str:
    """Each non-empty line must be JSON. Empty file is valid empty JSONL."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return SIDECAR_MISSING
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            return SIDECAR_INVALID_JSON
    return SIDECAR_OK


def copy_sidecar(src_video: Path, dest_video: Path) -> str:
    """Copy ``<stem>.jsonl`` beside *src_video* next to *dest_video*.

    Returns ``ok`` / ``missing`` / ``invalid_json``. Copies a defective file
    (status ``invalid_json``). Does not copy ``.events.jsonl``.
    """
    src = sidecar_source_path(src_video)
    dest = dest_video.with_suffix(".jsonl")
    if src.name.endswith(EVENTS_JSONL_SUFFIX) or dest.name.endswith(EVENTS_JSONL_SUFFIX):
        return SIDECAR_MISSING
    if not src.is_file():
        return SIDECAR_MISSING
    status = classify_jsonl(src)
    if src.resolve() != dest.resolve():
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    return status


def sidecar_counts(events: list[dict]) -> dict[str, int]:
    counts = {status: 0 for status in SIDECAR_STATUSES}
    for event in events:
        status = event.get("sidecar")
        if status in counts:
            counts[status] += 1
    return counts
