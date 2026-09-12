from pathlib import Path

import pytest

from tradevidanalyser.naming import FilenameError, parse_obs_filename


def test_obs_filename_space_and_dashes() -> None:
    session_id, start = parse_obs_filename("2026-09-11 14-30-00.mp4")
    assert session_id == "2026-09-11_143000"
    assert start.year == 2026
    assert start.month == 9
    assert start.day == 11
    assert start.hour == 14
    assert start.tzinfo is not None


def test_obs_filename_from_path() -> None:
    session_id, _ = parse_obs_filename(Path("/nas/recordings/2026-09-11 09-05-33.mkv"))
    assert session_id == "2026-09-11_090533"


def test_rejects_unstructured_name() -> None:
    with pytest.raises(FilenameError):
        parse_obs_filename("session.mp4")
