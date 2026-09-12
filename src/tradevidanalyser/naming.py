"""OBS filename `%CCYY-%MM-%DD %hh-%mm-%ss` → session id and Vienna start time."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

VIENNA = ZoneInfo("Europe/Vienna")

# OBS: 2026-09-11 14-30-00.mp4  (space or T; time with - or :)
_OBS_NAME = re.compile(
    r"(?P<date>\d{4}-\d{2}-\d{2})[ T_]"
    r"(?P<h>\d{2})[-:](?P<m>\d{2})[-:](?P<s>\d{2})"
)


class FilenameError(ValueError):
    pass


def parse_obs_filename(path: Path | str) -> tuple[str, datetime]:
    """Return (session_id, start_wallclock_vienna).

    Session id is ``YYYY-MM-DD_HHMMSS`` so two recordings on one day do not collide.
    The trading-session (ETH 18:00 ET) convention is documented; v1 uses the
    filename calendar date in the Vienna clock the PC was set to.
    """
    name = Path(path).stem
    match = _OBS_NAME.search(name)
    if not match:
        raise FilenameError(
            f"filename {Path(path).name!r} is not OBS `%CCYY-%MM-%DD %hh-%mm-%ss`"
        )
    date = match.group("date")
    h, m, s = match.group("h"), match.group("m"), match.group("s")
    naive = datetime.fromisoformat(f"{date}T{h}:{m}:{s}")
    start = naive.replace(tzinfo=VIENNA)
    session_id = f"{date}_{h}{m}{s}"
    return session_id, start
