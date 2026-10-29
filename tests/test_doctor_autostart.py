from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tradevidanalyser.cli import main
from tradevidanalyser.doctor import (
    SCHTASKS_QUERY,
    TVA_WATCH_CHECK_ID,
    autostart_check,
    run_doctor,
)

REPO = Path(__file__).resolve().parents[1]
WATCH_XML = REPO / "scripts" / "windows" / "tva-watch.xml"
WATCH_PLIST = REPO / "scripts" / "mac" / "com.tva.watch.plist"
DOCTOR_SRC = (REPO / "src" / "tradevidanalyser" / "doctor.py").read_text(encoding="utf-8")


def _fake_schtasks(
    stdout: str = "",
    *,
    returncode: int = 0,
    stderr: str = "",
    calls: list | None = None,
) -> object:
    def fake(cmd, *args, **kwargs):  # type: ignore[no-untyped-def]
        if calls is not None:
            calls.append((list(cmd), dict(kwargs)))
        return subprocess.CompletedProcess(list(cmd), returncode, stdout=stdout, stderr=stderr)

    return fake


def test_templates_have_altrading_header() -> None:
    xml = WATCH_XML.read_text(encoding="utf-8")
    plist = WATCH_PLIST.read_text(encoding="utf-8")
    assert xml.splitlines()[1].startswith("<!-- nicht für ALTrading (B.5)")
    assert plist.splitlines()[1].startswith("<!-- nicht für ALTrading (B.5)")


def test_doctor_source_never_mutates_tasks() -> None:
    forbidden = ("/Create", "/Change", "/Delete", "/End", "/Run", "shell=True")
    for token in forbidden:
        assert token not in DOCTOR_SRC
    assert 'SCHTASKS_QUERY = ["schtasks", "/Query"]' in DOCTOR_SRC


def test_autostart_skipped_off_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: False)
    called = {"n": 0}

    def boom(*_args, **_kwargs):
        called["n"] += 1
        raise AssertionError("schtasks must not run off Windows")

    monkeypatch.setattr("tradevidanalyser.doctor._query_scheduled_tasks", boom)
    check = autostart_check()
    assert check.id == TVA_WATCH_CHECK_ID
    assert check.status == "ok"
    assert "skipped" in check.detail
    assert called["n"] == 0


def test_autostart_no_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)
    monkeypatch.setattr(
        "tradevidanalyser.doctor.subprocess.run",
        _fake_schtasks("Folder: \\\nTaskName  Next Run Time  Status\nAdobe Update  N/A  Ready\n"),
    )
    check = autostart_check()
    assert check.status == "ok"
    assert check.detail == "no tva-watch scheduled task"


def test_autostart_reports_registered_task(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)
    monkeypatch.setattr(
        "tradevidanalyser.doctor.subprocess.run",
        _fake_schtasks(
            "Folder: \\\nTaskName                                 Next Run Time          Status\n"
            "TVA Watch                                N/A                    Ready\n"
        ),
    )
    check = autostart_check()
    assert check.status == "warn"
    assert "registered tva-watch" in check.detail


def test_autostart_reports_hyphenated_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)
    monkeypatch.setattr(
        "tradevidanalyser.doctor.subprocess.run",
        _fake_schtasks("tva-watch\n"),
    )
    assert autostart_check().status == "warn"


def test_autostart_query_uses_list_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)
    calls: list = []
    monkeypatch.setattr("tradevidanalyser.doctor.subprocess.run", _fake_schtasks("", calls=calls))
    autostart_check()
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv == SCHTASKS_QUERY
    assert all(isinstance(part, str) for part in argv)
    assert kwargs.get("shell") is False
    assert kwargs.get("check") is False
    assert kwargs.get("capture_output") is True


def test_autostart_query_error_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("schtasks")

    monkeypatch.setattr("tradevidanalyser.doctor.subprocess.run", missing)
    check = autostart_check()
    assert check.status == "warn"
    assert "failed" in check.detail


def test_autostart_nonzero_exit_is_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tradevidanalyser.doctor._is_windows", lambda: True)
    monkeypatch.setattr(
        "tradevidanalyser.doctor.subprocess.run",
        _fake_schtasks("ERROR: Access is denied.", returncode=1),
    )
    check = autostart_check()
    assert check.status == "warn"
    assert "exit 1" in check.detail


def test_run_doctor_includes_autostart(tva_root: Path) -> None:
    report = run_doctor(tva_root)
    ids = {item.id: item for item in report.checks}
    assert TVA_WATCH_CHECK_ID in ids
    assert ids[TVA_WATCH_CHECK_ID].status == "ok"
    assert report.ok


def test_cli_doctor_reports_tva_watch(tva_root: Path, capsys) -> None:
    code = main(["--root", str(tva_root), "doctor"])
    assert code == 0
    out = capsys.readouterr().out
    assert TVA_WATCH_CHECK_ID in out
    assert "tva_watch" in out
