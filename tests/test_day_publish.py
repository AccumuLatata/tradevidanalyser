from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tradevidanalyser import config, store
from tradevidanalyser.cli import main
from tradevidanalyser.day_manifest import (
    LOCK_ERROR,
    DayManifestError,
    build_day,
    day_lock_path,
)
from tradevidanalyser.day_publish import day_publish_path, debrief_missing_error
from tradevidanalyser.flags import (
    ENV_DAY_MANIFEST,
    ENV_DAY_PUBLISH,
    ENV_DAY_RULES,
    ENV_EXCLUSIVE_FILLS,
    ENV_PAUSE_GUARD,
    ENV_TRADING_HOURS,
    day_publish_enabled,
)
from tradevidanalyser.pause_guard import PauseCheck, write_pause_check
from tradevidanalyser.pipeline import publish_session as pipeline_publish
from tradevidanalyser.publish import (
    DebriefPayload,
    FakePublishClient,
    PublishError,
    debrief_title,
    payload_from_debrief,
    publish_session,
)
from tradevidanalyser.schema import (
    Alignment,
    DebriefReport,
    DebriefSection,
    RecordingInfo,
    SessionRecord,
)
from tradevidanalyser.watch import release_lock, try_acquire_lock

L0_DIR = Path(__file__).parent / "fixtures" / "l0_main_f44d864"
L0_SESSION = "2026-05-14_160300"
VIENNA = timezone(timedelta(hours=2))
DAY = date(2026, 9, 14)
CSV_HEADER = (
    "date,symbol,side,currency,underlying,asset_type,price,quantity,"
    "commission,fees,stop_loss,profit_target,tags,notes,spread_id"
)


def _four_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        ENV_PAUSE_GUARD,
        ENV_DAY_MANIFEST,
        ENV_EXCLUSIVE_FILLS,
        ENV_DAY_RULES,
        ENV_DAY_PUBLISH,
        ENV_TRADING_HOURS,
    ):
        monkeypatch.setenv(name, "1")


def _session(
    root: Path,
    session_id: str,
    *,
    start: datetime,
    duration_s: float,
    sha256: str | None = None,
    clock_resolution_s: int = 1,
    pause_check: str = "clear",
) -> SessionRecord:
    name = f"{session_id[:10]} {session_id[11:13]}-{session_id[13:15]}-{session_id[15:17]}.mp4"
    record = SessionRecord(
        id=session_id,
        recording=RecordingInfo(
            path=f"recordings/{name}",
            sha256=sha256 or ("a" * 64),
            start_wallclock_vienna=start.isoformat(),
            duration_s=duration_s,
            filename=name,
        ),
    )
    store.save_session(root, record)
    store.compute_status(root, record.id)
    write_pause_check(
        root,
        PauseCheck(
            session_id=record.id,
            recording_sha256=record.recording.sha256,
            part_shas=[],
            duration_s=float(record.recording.duration_s),
            pause_check=pause_check,  # type: ignore[arg-type]
            pause_total_s=0.0,
            clock_resolution_s=clock_resolution_s,
            pause_detectable_from_s=30.0,
            ocr_provider="injected",
            ocr_model="test",
        ),
    )
    return record


def _write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(CSV_HEADER + "\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return path


def _win(entry: str, exit_ts: str, *, spread: str, price: str = "21000.0") -> list[str]:
    return [
        f"{entry},MNQM26,buy,USD,MNQ,future,{price},1.0,0,0,N/A,N/A,,,{spread}",
        (f"{exit_ts},MNQM26,sell,USD,MNQ,future,{float(price) + 1},1.0,0,0,N/A,N/A,,,{spread}"),
    ]


def _two_clips(root: Path) -> tuple[SessionRecord, SessionRecord]:
    a = _session(
        root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    b = _session(
        root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    return a, b


def _write_debrief(
    root: Path,
    session_id: str,
    *,
    summaries: str = "Reviewed the tape against T01. Extra clause stays out.",
    learnings: str = "Name the playbook before entry.\nState stop and target.\nCool down after tilt.",
) -> None:
    report = DebriefReport(
        session_id=session_id,
        provider="fake",
        model="none",
        prompt_version="debrief-fake-v1",
        sections=[
            DebriefSection(id="day", title="Day", kind="prose", body=summaries),
            DebriefSection(id="learnings", title="Learnings", kind="prose", body=learnings),
        ],
    )
    store.write_json(store.debrief_json_path(root, session_id), report.model_dump(mode="json"))
    store.debrief_md_path(root, session_id).write_text("# Debrief\n", encoding="utf-8")


def _build_two(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[SessionRecord, SessionRecord]:
    _four_on(monkeypatch)
    a, b = _two_clips(root)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(root, DAY, executions=csv, venue="amp")
    return a, b


def test_day_publish_flag_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ENV_DAY_PUBLISH, raising=False)
    assert day_publish_enabled() is False
    for raw in ("", "0", "false", "off", "no"):
        monkeypatch.setenv(ENV_DAY_PUBLISH, raw)
        assert day_publish_enabled() is False


def test_two_sessions_one_page_id_second_payload_is_not_clip_debrief(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _build_two(tva_root, tmp_path, monkeypatch)
    _write_debrief(
        tva_root,
        a.id,
        summaries="Clip A only sentence. Extra.",
        learnings="Alpha one.\nAlpha two.",
    )
    _write_debrief(
        tva_root,
        b.id,
        summaries="Clip B only sentence. Extra.",
        learnings="Bravo one.\nBravo two.",
    )
    first = publish_session(a.id, root=tva_root, notion=True)
    second = publish_session(b.id, root=tva_root, notion=True)
    assert first.page_id == second.page_id
    assert first.created is True
    assert second.created is False
    assert "replaced_existing_page" not in second.as_dict()
    day_art = store.read_json(day_publish_path(tva_root, DAY))
    assert day_art["page_id"] == first.page_id
    assert store.read_json(store.publish_path(tva_root, a.id))["page_id"] == first.page_id
    fake = FakePublishClient(tva_root)
    matches = [page for page in fake._pages.values() if page.title == debrief_title(a)]
    assert len(matches) == 1
    page = matches[0]
    assert page.properties["Summaries"] != "Clip B only sentence."
    assert page.properties["Summaries"] != "Clip A only sentence."
    assert "2 clips" in page.properties["Summaries"]
    assert "0 clock_resolution_s=60" in page.properties["Summaries"]
    assert "trades_outside_clips" in page.properties["Summaries"]
    assert page.properties["Learning 1"] == "Alpha one."
    assert page.properties["Learning 2"] == "Alpha two."
    assert page.properties["Learning 3"] == "Bravo one."
    assert debrief_title(a) == debrief_title(b) == "14 Sep 2026 Session Debrief"
    assert first.as_dict()["page_id"] == day_art["page_id"]


def test_m22_missing_debrief_aborts_and_leaves_page(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _build_two(tva_root, tmp_path, monkeypatch)
    title = debrief_title(a)
    existing = FakePublishClient(tva_root).create_debrief(
        DebriefPayload(
            title=title,
            summaries="Reviewed the tape against T01.",
            learnings=[
                "Name the playbook before entry.",
                "State stop and target.",
                "Cool down after tilt.",
            ],
        )
    )
    with pytest.raises(PublishError, match="Debrief fehlt für") as exc:
        publish_session(a.id, root=tva_root, notion=True)
    assert str(exc.value) == debrief_missing_error([a.id, b.id])
    assert not day_publish_path(tva_root, DAY).is_file()
    assert not store.publish_path(tva_root, a.id).is_file()
    page = FakePublishClient(tva_root).get_page(existing.page_id)
    assert page is not None
    assert page.properties["Summaries"] == "Reviewed the tape against T01."
    assert page.properties["Learning 1"] == "Name the playbook before entry."
    assert main(["--root", str(tva_root), "publish", a.id, "--notion"]) == 1


def test_first_day_publish_replaces_existing_clip_page(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_clips(tva_root)
    _write_debrief(tva_root, a.id)
    prior = publish_session(a.id, root=tva_root, notion=True)
    assert prior.created is True
    assert "replaced_existing_page" not in prior.as_dict()
    _four_on(monkeypatch)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    _write_debrief(
        tva_root,
        a.id,
        summaries="Clip A only sentence. Extra.",
        learnings="Alpha one.\nAlpha two.",
    )
    _write_debrief(
        tva_root,
        b.id,
        summaries="Clip B only sentence. Extra.",
        learnings="Bravo one.",
    )
    result = publish_session(a.id, root=tva_root, notion=True)
    assert result.page_id == prior.page_id
    assert result.created is False
    assert result.replaced_existing_page is True
    assert result.as_dict()["replaced_existing_page"] is True
    fake = FakePublishClient(tva_root)
    page = fake.get_page(prior.page_id)
    assert page is not None
    assert "2 clips" in page.properties["Summaries"]
    assert page.properties["Summaries"] != "Reviewed the tape against T01."


def test_missing_one_debrief_leaves_existing_page(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _two_clips(tva_root)
    _write_debrief(tva_root, a.id)
    prior = publish_session(a.id, root=tva_root, notion=True)
    _four_on(monkeypatch)
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T07:05:00+0000", "2026-09-14T07:06:00+0000", spread="a"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    store.debrief_json_path(tva_root, a.id).unlink()
    with pytest.raises(PublishError, match="Debrief fehlt für"):
        publish_session(b.id, root=tva_root, notion=True)
    assert not day_publish_path(tva_root, DAY).is_file()
    page = FakePublishClient(tva_root).get_page(prior.page_id)
    assert page is not None
    assert page.properties["Summaries"] == "Reviewed the tape against T01."


def test_l0_payload_identity_with_four_set(tva_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _four_on(monkeypatch)
    src = L0_DIR / "l0-ocr"
    dest = config.session_dir(tva_root, L0_SESSION)
    dest.mkdir(parents=True)
    for name in ("session.json", "debrief.json", "debrief.md"):
        shutil.copy2(src / name, dest / name)
    record = store.load_session(tva_root, L0_SESSION)
    write_pause_check(
        tva_root,
        PauseCheck(
            session_id=record.id,
            recording_sha256=record.recording.sha256,
            part_shas=[],
            duration_s=float(record.recording.duration_s),
            pause_check="clear",
            pause_total_s=0.0,
            clock_resolution_s=1,
            pause_detectable_from_s=30.0,
            ocr_provider="injected",
            ocr_model="test",
        ),
    )
    want = json.loads((src / "notion_payload.json").read_text(encoding="utf-8"))
    report = DebriefReport.model_validate(
        store.read_json(store.debrief_json_path(tva_root, L0_SESSION))
    )
    payload = payload_from_debrief(record, report)
    assert payload.title == want["title"]
    assert payload.summaries == want["summaries"]
    assert list(payload.learnings) == want["learnings"]
    result = publish_session(L0_SESSION, root=tva_root, notion=True)
    assert "replaced_existing_page" not in result.as_dict()
    assert not (tva_root / "days").exists()
    fake = FakePublishClient(tva_root)
    page = fake.find_page(want["title"])
    assert page is not None
    assert page.properties["Summaries"] == want["summaries"]
    assert page.properties["Learning 1"] == want["learnings"][0]
    assert page.properties["Learning 2"] == want["learnings"][1]
    assert page.properties["Learning 3"] == want["learnings"][2]
    assert store.publish_path(tva_root, L0_SESSION).is_file()
    assert not day_publish_path(tva_root, date(2026, 5, 14)).is_file()


def test_flag_off_second_clip_still_overwrites_title(
    tva_root: Path,
) -> None:
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
    )
    b = _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
    )
    _write_debrief(tva_root, a.id, summaries="First clip sentence. Extra.")
    _write_debrief(tva_root, b.id, summaries="Second clip sentence. Extra.")
    first = publish_session(a.id, root=tva_root, notion=True)
    second = publish_session(b.id, root=tva_root, notion=True)
    assert first.page_id == second.page_id
    fake = FakePublishClient(tva_root)
    page = fake.get_page(first.page_id)
    assert page is not None
    assert page.properties["Summaries"] == "Second clip sentence."
    assert not day_publish_path(tva_root, DAY).is_file()


def test_concurrent_publish_one_gets_lock(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _build_two(tva_root, tmp_path, monkeypatch)
    _write_debrief(tva_root, a.id, learnings="Alpha one.")
    _write_debrief(tva_root, b.id, learnings="Bravo one.")
    lock = day_lock_path(tva_root, DAY)
    assert try_acquire_lock(lock)
    try:
        with pytest.raises(DayManifestError, match=LOCK_ERROR):
            pipeline_publish(a.id, root=tva_root, notion=True)
        assert not day_publish_path(tva_root, DAY).is_file()
        fake = FakePublishClient(tva_root)
        assert fake.find_page(debrief_title(a)) is None
    finally:
        release_lock(lock)


def test_alignment_invalid_word_and_minute_clock_count(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _four_on(monkeypatch)
    a = _session(
        tva_root,
        "2026-09-14_090000",
        start=datetime(2026, 9, 14, 9, 0, tzinfo=VIENNA),
        duration_s=600.0,
        clock_resolution_s=60,
        pause_check="suspected",
    )
    a = a.model_copy(
        update={
            "alignment": Alignment(
                offset_s=0.0,
                drift_s_per_h=0.0,
                confidence=0.0,
                method="invalid",
                samples=[],
            )
        }
    )
    store.save_session(tva_root, a)
    b = _session(
        tva_root,
        "2026-09-14_110000",
        start=datetime(2026, 9, 14, 11, 0, tzinfo=VIENNA),
        duration_s=600.0,
        sha256="b" * 64,
        clock_resolution_s=1,
    )
    csv = _write_csv(
        tmp_path / "e.csv",
        _win("2026-09-14T09:05:00+0000", "2026-09-14T09:06:00+0000", spread="b"),
    )
    build_day(tva_root, DAY, executions=csv, venue="amp")
    _write_debrief(tva_root, a.id, learnings="Alpha one.")
    _write_debrief(tva_root, b.id, learnings="Bravo one.")
    result = publish_session(a.id, root=tva_root, notion=True)
    page = FakePublishClient(tva_root).get_page(result.page_id or "")
    assert page is not None
    sentence = page.properties["Summaries"]
    assert "alignment_invalid" in sentence
    assert "1 clock_resolution_s=60" in sentence
    assert "1 suspected" in sentence
    assert "video time" not in sentence.lower()
    assert "wall_to_video" not in sentence


def test_session_publish_json_is_not_page_id_source(
    tva_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = _build_two(tva_root, tmp_path, monkeypatch)
    _write_debrief(tva_root, a.id, learnings="Alpha one.")
    _write_debrief(tva_root, b.id, learnings="Bravo one.")
    first = publish_session(a.id, root=tva_root, notion=True)
    stale = store.read_json(store.publish_path(tva_root, a.id))
    stale["page_id"] = "fake-stale-not-used"
    store.write_json(store.publish_path(tva_root, b.id), stale)
    second = publish_session(b.id, root=tva_root, notion=True)
    assert second.page_id == first.page_id
    assert second.page_id != "fake-stale-not-used"
    day_art = store.read_json(day_publish_path(tva_root, DAY))
    assert day_art["page_id"] == first.page_id
