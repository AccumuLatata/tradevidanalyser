from __future__ import annotations

from datetime import date
from pathlib import Path

import httpx
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from tradevidanalyser import store
from tradevidanalyser.cli import main
from tradevidanalyser.coach import (
    ENV_COACH,
    ENV_XAI_KEY,
    CoachDraft,
    CoachError,
    FakeCoachProvider,
    _load_debriefs,
    accept_experiment,
    coach_session,
    gather_pack,
    get_coach_provider,
    instance_cite_ids,
    load_latest,
    min_citations,
)
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ledger import add_session, list_experiments
from tradevidanalyser.schema import (
    CoachClaim,
    CoachExperiment,
    CoachReport,
    DebriefReport,
    DebriefSection,
    RecordingInfo,
    SessionRecord,
)
from tradevidanalyser.serve import ALLOWED_RUN_STAGES, create_app


def _session(
    root: Path,
    session_id: str = "2026-09-11_143000",
    *,
    start: str | None = None,
) -> SessionRecord:
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{session_id.replace('_', ' ')}.mp4",
            sha256="0" * 64,
            start_wallclock_vienna=start or f"{session_id[:10]}T14:30:00+02:00",
            duration_s=7200.0,
            filename=f"{session_id.replace('_', ' ')}.mp4",
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    return record


def _write_trades(root: Path, session_id: str, n: int = 10) -> None:
    ids = [f"T{i:02d}" for i in range(1, n + 1)]
    table = pa.table(
        {
            "tva_trade_id": ids,
            "trade_id": [f"jt-{i}" for i in ids],
            "entry_fill_id": [f"fill-{i}" for i in ids],
            "direction": ["long"] * n,
            "instrument": ["MNQ"] * n,
            "entry_price": [21000.0] * n,
            "exit_price": [20990.0] * n,
            "net_pnl_currency": [-10.0] * n,
            "status": ["closed"] * n,
            "venue": ["amp"] * n,
        }
    )
    path = store.trades_path(root, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path)


def _write_rules(root: Path, session_id: str, *, status: str = "violated") -> None:
    store.write_json(
        store.rules_path(root, session_id),
        {
            "schema_version": "1",
            "session_id": session_id,
            "rules": [{"rule": "R-PLAYBOOK", "status": status, "evidence": {}, "reason": None}],
        },
    )


def _seed_ledger(
    root: Path,
    *,
    trades: int = 10,
    session_id: str = "2026-09-11_143000",
    start: str | None = None,
) -> str:
    record = _session(root, session_id, start=start)
    _write_trades(root, record.id, trades)
    _write_rules(root, record.id)
    add_session(record.id, root=root)
    return record.id


def test_min_citations_defaults_to_ten(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TVA_COACH_MIN_N", raising=False)
    assert min_citations() == 10


def test_claim_citation_count_enforced(tva_root: Path) -> None:
    session_id = _seed_ledger(tva_root, trades=10)
    pack = gather_pack(tva_root, weeks=4, min_n=10)
    short = CoachClaim(
        text="Too thin.",
        cites=pack.row_ids[:2],
    )
    result = coach_session(
        root=tva_root,
        weeks=4,
        min_n=10,
        provider=FakeCoachProvider(CoachDraft(claims=[short])),
    )
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims == []
    assert any("need >=10" in gap for gap in report.gaps)
    assert result.claims == 0
    assert session_id in pack.session_ids


def test_fabricated_ledger_cite_is_dropped(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=10)
    pack = gather_pack(tva_root, weeks=4, min_n=3)
    bogus = CoachClaim(
        text="Invented row.",
        cites=[*pack.row_ids[:2], "NOPE:T99"],
    )
    coach_session(
        root=tva_root,
        weeks=4,
        min_n=3,
        provider=FakeCoachProvider(CoachDraft(claims=[bogus])),
    )
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims == []
    assert any("fabricated" in gap for gap in report.gaps)


def test_ten_instance_claim_and_experiment_schema(tva_root: Path) -> None:
    session_id = _seed_ledger(tva_root, trades=10)
    result = coach_session(root=tva_root, weeks=4, min_n=10)
    assert result.status == "ok"
    assert result.claims == 1
    report = load_latest(tva_root)
    assert report is not None
    assert report.min_citations == 10
    assert len(report.claims) == 1
    claim = report.claims[0]
    assert len(claim.cites) >= 10
    assert all(cite in gather_pack(tva_root, weeks=4, min_n=10).row_ids for cite in claim.cites)
    assert any(cite.endswith(":T01") or cite == f"{session_id}:T01" for cite in claim.cites)
    exp = report.experiment
    assert exp is not None
    dumped = exp.model_dump(mode="json")
    assert dumped["rule_change"]
    assert dumped["start"]
    assert dumped["stop_criterion"]
    assert dumped["status"] == "running"
    assert list_experiments(tva_root) == [exp]
    assert store.coach_md_path(tva_root).is_file()
    assert "stop_criterion" in store.coach_md_path(tva_root).read_text(encoding="utf-8")


def test_experiment_requires_stop_criterion(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=10)
    pack = gather_pack(tva_root, weeks=4, min_n=3)
    draft = CoachDraft(
        claims=[CoachClaim(text="Repeats across three rows.", cites=pack.row_ids[:3])],
        experiment=CoachExperiment(rule_change="Name the playbook", start="2026-09-11", stop_criterion=""),
    )
    coach_session(root=tva_root, weeks=4, min_n=3, provider=FakeCoachProvider(draft))
    report = load_latest(tva_root)
    assert report is not None
    assert report.experiment is None
    assert list_experiments(tva_root) == []
    assert any("stop_criterion" in gap for gap in report.gaps)


def test_at_most_one_experiment_on_rerun(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=10)
    first = coach_session(root=tva_root, weeks=4, min_n=10)
    second = coach_session(root=tva_root, weeks=4, min_n=10)
    assert first.experiment_id == second.experiment_id == "E01"
    assert len(list_experiments(tva_root)) == 1


def test_last_n_debriefs_are_loaded(tva_root: Path) -> None:
    ids = [
        "2026-08-20_143000",
        "2026-09-01_143000",
        "2026-09-08_143000",
        "2026-09-10_143000",
        "2026-09-11_143000",
    ]
    for session_id in ids:
        _seed_ledger(tva_root, trades=2, session_id=session_id)
        store.write_json(
            store.debrief_json_path(tva_root, session_id),
            DebriefReport(
                session_id=session_id,
                provider="fake",
                model="none",
                prompt_version="debrief-fake-v1",
                sections=[DebriefSection(id="day", title="Day", kind="prose", body=f"Day {session_id}")],
            ).model_dump(mode="json"),
        )
    pack = gather_pack(tva_root, weeks=2, min_n=2)
    assert [item["session_id"] for item in pack.debriefs] == ["2026-09-10_143000", "2026-09-11_143000"]


def test_api_coach_latest_404_then_ok(tva_root: Path) -> None:
    client = TestClient(create_app(tva_root))
    missing = client.get("/coach/latest")
    assert missing.status_code == 404
    _seed_ledger(tva_root, trades=10)
    coach_session(root=tva_root, weeks=4, min_n=10)
    body = client.get("/coach/latest").json()
    CoachReport.model_validate(body)
    assert body["claims"]
    assert body["experiment"]["stop_criterion"]
    openapi = client.get("/openapi.json").json()
    assert "/coach/latest" in openapi["paths"]


def test_coach_not_in_bot_run_stages() -> None:
    assert ALLOWED_RUN_STAGES == ("transcribe", "extract")
    assert "coach" not in ALLOWED_RUN_STAGES


def test_cli_coach(tva_root: Path, capsys) -> None:
    _seed_ledger(tva_root, trades=10)
    assert main(["--root", str(tva_root), "coach", "--weeks", "4"]) == 0
    out = capsys.readouterr().out
    assert "coach/latest.json" in out
    assert main(["--root", str(tva_root), "coach", "--weeks", "0"]) == 1


def test_grok_without_key_fails(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    _seed_ledger(tva_root, trades=3)
    with pytest.raises(CoachError, match="XAI_API_KEY"):
        coach_session(root=tva_root, weeks=4, min_n=3, provider_name="grok")


def test_fake_never_calls_live_xai(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_COACH, raising=False)
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    _seed_ledger(tva_root, trades=10)

    def boom(*_args, **_kwargs):
        raise AssertionError("live xAI must not be called in CI")

    monkeypatch.setattr("httpx.Client.post", boom)
    result = coach_session(root=tva_root, weeks=4, min_n=10)
    assert result.provider == "fake"


def test_unknown_provider_is_error() -> None:
    with pytest.raises(CoachError, match="unknown coach provider"):
        get_coach_provider("claude")


def test_doctor_coach_provider(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_COACH, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["coach_provider"].status == "ok"
    monkeypatch.setenv(ENV_COACH, "grok")
    monkeypatch.delenv(ENV_XAI_KEY, raising=False)
    ids = {item.id: item for item in run_doctor(tva_root).checks}
    assert ids["coach_provider"].status == "fail"


def test_grok_mock_is_still_citation_checked(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_XAI_KEY, "test-key")
    _seed_ledger(tva_root, trades=10)
    pack = gather_pack(tva_root, weeks=4, min_n=10)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": {
                                "claims": [
                                    {
                                        "text": "Invented cite.",
                                        "cites": ["NOPE:T99"],
                                        "question": "",
                                    },
                                    {
                                        "text": "Repeats across ten ledger rows.",
                                        "cites": pack.row_ids[:10],
                                        "question": "Name the playbook?",
                                    },
                                ],
                                "experiment": {
                                    "rule_change": "State the playbook before entry",
                                    "start": "2026-09-11",
                                    "stop_criterion": "Stop after 10 sessions",
                                },
                            }
                        }
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    result = coach_session(
        root=tva_root, weeks=4, min_n=10, provider_name="grok", client=client
    )
    report = load_latest(tva_root)
    assert result.provider == "grok"
    assert report is not None
    assert len(report.claims) == 1
    assert report.claims[0].cites == pack.row_ids[:10]
    assert any("fabricated" in gap for gap in report.gaps)
    assert report.experiment is not None
    assert report.experiment.stop_criterion


def test_thin_session_id_padding_is_dropped(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=3)
    pack = gather_pack(tva_root, weeks=4, min_n=5)
    # session + 3 trades + 1 rule = 5 allow-listed ids, but only 4 instance rows
    assert len(pack.row_ids) >= 5
    assert len(instance_cite_ids(pack)) == 4
    coach_session(
        root=tva_root,
        weeks=4,
        min_n=5,
        provider=FakeCoachProvider(
            CoachDraft(claims=[CoachClaim(text="Padded with the session id.", cites=pack.row_ids)])
        ),
    )
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims == []
    assert any("need >=5" in gap for gap in report.gaps)


def test_invented_claim_digit_is_dropped(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=5)
    pack = gather_pack(tva_root, weeks=4, min_n=5)
    instances = sorted(instance_cite_ids(pack))
    coach_session(
        root=tva_root,
        weeks=4,
        min_n=5,
        provider=FakeCoachProvider(
            CoachDraft(
                claims=[
                    CoachClaim(
                        text="Adherence is 87 percent across these rows.",
                        cites=instances,
                    )
                ]
            )
        ),
    )
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims == []
    assert any("invented digit" in gap for gap in report.gaps)


def test_start_date_cannot_launder_experiment_digits(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=5)
    pack = gather_pack(tva_root, weeks=4, min_n=5)
    instances = sorted(instance_cite_ids(pack))
    coach_session(
        root=tva_root,
        weeks=4,
        min_n=5,
        provider=FakeCoachProvider(
            CoachDraft(
                claims=[CoachClaim(text="Repeats across five ledger rows.", cites=instances)],
                experiment=CoachExperiment(
                    rule_change="Cap loss at 2099",
                    start="2099-01-01",
                    stop_criterion="Stop at 2099",
                ),
            )
        ),
    )
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims
    assert report.experiment is None
    assert list_experiments(tva_root) == []
    assert any("invented digit" in gap for gap in report.gaps)


def test_empty_ledger_writes_no_experiment(tva_root: Path) -> None:
    result = coach_session(root=tva_root, weeks=4, min_n=10)
    assert result.status == "ok"
    assert result.claims == 0
    report = load_latest(tva_root)
    assert report is not None
    assert report.claims == []
    assert report.experiment is None
    assert list_experiments(tva_root) == []
    assert any("citeable rows" in gap for gap in report.gaps)
    client = TestClient(create_app(tva_root))
    body = client.get("/coach/latest")
    assert body.status_code == 200
    assert body.json()["claims"] == []


def test_week_window_excludes_sessions_outside_trailing_iso_weeks(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=2, session_id="2026-08-01_143000")
    _seed_ledger(tva_root, trades=2, session_id="2026-09-11_143000")
    pack = gather_pack(tva_root, weeks=4, min_n=2)
    assert "2026-09-11_143000" in pack.session_ids
    assert "2026-08-01_143000" not in pack.session_ids
    assert all(not cite.startswith("2026-08-01_143000") for cite in pack.row_ids)


def test_utc_wallclock_uses_vienna_iso_week(tva_root: Path) -> None:
    # 22:30 UTC Sunday 13 Sep is 00:30 Vienna Monday 14 Sep (2026-W38).
    _seed_ledger(
        tva_root,
        trades=2,
        session_id="2026-09-13_223000",
        start="2026-09-13T22:30:00+00:00",
    )
    pack = gather_pack(tva_root, weeks=1, min_n=2)
    assert pack.session_ids == ["2026-09-13_223000"]


def test_experiment_start_uses_vienna_today(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _seed_ledger(tva_root, trades=3)
    monkeypatch.setattr("tradevidanalyser.coach.vienna_today", lambda: date(2026, 9, 14))
    monkeypatch.setattr("tradevidanalyser.coach.date.today", lambda: date(2026, 9, 13))
    pack = gather_pack(tva_root, weeks=4, min_n=3)
    instances = sorted(instance_cite_ids(pack))
    draft = CoachDraft(
        claims=[CoachClaim(text="Repeats across three ledger rows.", cites=instances)],
        experiment=CoachExperiment(
            rule_change="State the playbook before entry",
            start="",
            stop_criterion="Stop after 4 weeks",
        ),
    )
    experiment, gaps = accept_experiment(draft, pack)
    assert gaps == []
    assert experiment is not None
    assert experiment.start == "2026-09-14"


def test_rerun_keeps_running_when_draft_proposes_another(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=10)
    first = coach_session(root=tva_root, weeks=4, min_n=10)
    pack = gather_pack(tva_root, weeks=4, min_n=10)
    draft = CoachDraft(
        claims=[
            CoachClaim(
                text="The same process miss repeats across 10 ledger rows.",
                cites=sorted(instance_cite_ids(pack)),
            )
        ],
        experiment=CoachExperiment(
            rule_change="Different rule entirely",
            start="2026-09-11",
            stop_criterion="Stop after 4 weeks",
        ),
    )
    second = coach_session(root=tva_root, weeks=4, min_n=10, provider=FakeCoachProvider(draft))
    assert first.experiment_id == second.experiment_id == "E01"
    held = list_experiments(tva_root)
    assert len(held) == 1
    assert held[0].rule_change != "Different rule entirely"


def test_no_claims_does_not_append_experiment(tva_root: Path) -> None:
    _seed_ledger(tva_root, trades=2)
    pack = gather_pack(tva_root, weeks=4, min_n=10)
    coach_session(
        root=tva_root,
        weeks=4,
        min_n=10,
        provider=FakeCoachProvider(
            CoachDraft(
                claims=[CoachClaim(text="Too thin.", cites=pack.row_ids[:2])],
                experiment=CoachExperiment(
                    rule_change="State the playbook before entry",
                    start="2026-09-11",
                    stop_criterion="Stop after 4 weeks",
                ),
            )
        ),
    )
    assert list_experiments(tva_root) == []
    report = load_latest(tva_root)
    assert report is not None
    assert report.experiment is None


def test_coach_refuses_dir_symlink_outside_root(tva_root: Path, tmp_path: Path) -> None:
    _seed_ledger(tva_root, trades=10)
    outside = tmp_path / "outside_coach"
    outside.mkdir()
    dest = store.coach_dir(tva_root)
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not permitted")
    with pytest.raises((CoachError, ValueError), match="TVA_ROOT"):
        coach_session(root=tva_root, weeks=4, min_n=10)
    assert not (outside / "latest.json").exists()
    assert load_latest(tva_root) is None
    client = TestClient(create_app(tva_root))
    assert client.get("/coach/latest").status_code == 404


def test_debrief_symlink_and_unsafe_session_id_are_skipped(
    tva_root: Path, tmp_path: Path
) -> None:
    session_id = _seed_ledger(tva_root, trades=2)
    outside = tmp_path / "leaked.json"
    outside.write_text(
        DebriefReport(
            session_id=session_id,
            provider="fake",
            model="none",
            prompt_version="debrief-fake-v1",
            sections=[DebriefSection(id="day", title="Day", kind="prose", body="Secret 777")],
        ).model_dump_json(),
        encoding="utf-8",
    )
    dest = store.debrief_json_path(tva_root, session_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not permitted")
    pack = gather_pack(tva_root, weeks=4, min_n=2)
    assert pack.debriefs == []
    assert "777" not in pack.allowed_runs
    assert _load_debriefs(tva_root, ["..", "foo/bar", session_id], limit=4) == []
