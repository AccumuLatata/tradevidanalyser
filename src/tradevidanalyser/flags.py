"""Day-path and guard flags. Unset/empty is off; truthy is 1/true/yes/on (plan §3.6)."""

from __future__ import annotations

import os

ENV_PAUSE_GUARD = "TVA_PAUSE_GUARD"
ENV_DAY_MANIFEST = "TVA_DAY_MANIFEST"
ENV_EXCLUSIVE_FILLS = "TVA_EXCLUSIVE_FILLS"
ENV_DAY_RULES = "TVA_DAY_RULES"
ENV_DAY_PUBLISH = "TVA_DAY_PUBLISH"
ENV_TRADING_HOURS = "TVA_TRADING_HOURS"
ENV_ALLOW_SESSION_ID_OVERWRITE = "TVA_ALLOW_SESSION_ID_OVERWRITE"

TRUTHY = frozenset({"1", "true", "yes", "on"})
INVALID_FLAG_SET_ERROR = "ungültige Flag-Kombination"

# The four day-path flags may only be on together with pause + manifest (plan §3.6).
_DAY_PATH_FOUR = (
    ENV_EXCLUSIVE_FILLS,
    ENV_DAY_RULES,
    ENV_DAY_PUBLISH,
    ENV_TRADING_HOURS,
)


def env_flag(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in TRUTHY


def pause_guard_enabled() -> bool:
    return env_flag(ENV_PAUSE_GUARD)


def day_manifest_enabled() -> bool:
    return env_flag(ENV_DAY_MANIFEST)


def active_flag_names() -> list[str]:
    names = [
        ENV_PAUSE_GUARD,
        ENV_DAY_MANIFEST,
        ENV_EXCLUSIVE_FILLS,
        ENV_DAY_RULES,
        ENV_DAY_PUBLISH,
        ENV_TRADING_HOURS,
    ]
    return [name for name in names if env_flag(name)]


def require_allowed_flag_set() -> None:
    """Abort on a combination that §3.6 does not list. Orthogonal overwrite is ignored."""
    pause = env_flag(ENV_PAUSE_GUARD)
    manifest = env_flag(ENV_DAY_MANIFEST)
    four = [env_flag(name) for name in _DAY_PATH_FOUR]
    four_any = any(four)
    four_all = all(four)
    if not pause and not manifest and not four_any:
        return
    if four_any:
        if not (pause and manifest and four_all):
            raise ValueError(INVALID_FLAG_SET_ERROR)
        return
    # only pause, only manifest, or pause+manifest
