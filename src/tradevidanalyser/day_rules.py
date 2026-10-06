"""Day-level rules and ledger tables (PR-33, plan §2.4)."""

from __future__ import annotations

import shutil
from datetime import date
from pathlib import Path

from tradevidanalyser import store
from tradevidanalyser.day_fills import DayOwnership
from tradevidanalyser.day_manifest import day_dir, load_current_day_json, nominal_vienna_date
from tradevidanalyser.flags import day_rules_enabled
from tradevidanalyser.rules import (
    RulesConfig,
    RulesResult,
    SCHEMA_VERSION,
    compose_day_session_rules,
    evaluate_day_rules,
    load_rules_config,
    read_rule_trades,
    rule_trades_from_journal,
)
from tradevidanalyser.schema import Evidence, RulesReport, SessionEvent, SessionRecord

DAY_RULES_NAME = "rules.json"


def apply_day_rules(
    root: Path,
    day: date,
    ownership: DayOwnership,
    build_dir: Path,
    clips: list[SessionRecord],
    *,
    config: RulesConfig | None = None,
    config_path: Path | None = None,
) -> None:
    """Rewrite every clip rules.json, write day rules, replace ledger day rows."""
    if not day_rules_enabled():
        return
    cfg = config or load_rules_config(config_path)
    day_trades = rule_trades_from_journal(ownership.trades, ownership.tva_trade_ids)
    day_checks = evaluate_day_rules(day_trades, cfg)
    store.write_json(
        build_dir / DAY_RULES_NAME,
        {
            "schema_version": SCHEMA_VERSION,
            "date": day.isoformat(),
            "rules": [item.model_dump(mode="json") for item in day_checks],
        },
    )
    from tradevidanalyser.ledger import (
        add_session,
        drop_day_rows,
        replace_rule_checks,
        session_recorded,
        upsert_day_rule_checks,
        upsert_day_rollup,
    )

    drop_day_rows(root, day)
    upsert_day_rule_checks(root, day, day_checks)
    entitled = sum(
        1 for trade in ownership.trades if ownership.fill_owner.get(trade.entry_fill_id)
    )
    outside = sum(
        1 for trade in ownership.trades if ownership.fill_owner.get(trade.entry_fill_id) is None
    )
    hours = sum(float(record.recording.duration_s or 0.0) for record in clips) / 3600.0
    upsert_day_rollup(
        root,
        day,
        trade_count=entitled,
        hours=hours,
        trades_outside_clips=outside,
        trades_per_hour_reason=None,
    )
    backup_root = build_dir / "cascade_backup"
    for record in clips:
        _backup_rules(root, record.id, backup_root / record.id)
        report = _session_day_report(record, root=root, day=day, config=cfg)
        store.write_json(store.rules_path(root, record.id), report.model_dump(mode="json"))
        if session_recorded(root, record.id):
            replace_rule_checks(root, record.id)
        else:
            add_session(record.id, root=root)
        store.compute_status(root, record.id)


def rules_session_day_path(
    record: SessionRecord,
    *,
    root: Path,
    config: RulesConfig | None = None,
    config_path: Path | None = None,
) -> RulesResult:
    """tva rules on a day-path session: write session + day artifacts, or stale already aborted."""
    cfg = config or load_rules_config(config_path)
    day = nominal_vienna_date(record)
    report = _session_day_report(record, root=root, day=day, config=cfg)
    store.write_json(store.rules_path(root, record.id), report.model_dump(mode="json"))
    _refresh_day_rules_file(root, day)
    store.drop_debrief(root, record.id)
    store.drop_ledger_session(root, record.id)
    store.compute_status(root, record.id)
    return RulesResult(
        session_id=record.id,
        status="ok",
        path="rules.json",
        rules=len(report.rules),
    )


def _session_day_report(
    record: SessionRecord,
    *,
    root: Path,
    day: date,
    config: RulesConfig,
) -> RulesReport:
    trades_file = store.trades_path(root, record.id)
    trades = read_rule_trades(trades_file) if trades_file.is_file() else []
    evidence = _load_evidence(root, record.id)
    events = _load_session_events(root, record.id)
    from tradevidanalyser.rules import record_session_date, session_start_utc

    return RulesReport(
        schema_version=SCHEMA_VERSION,
        session_id=record.id,
        rules=compose_day_session_rules(
            trades,
            config,
            day=day,
            session_date=record_session_date(record),
            evidence=evidence,
            session_events=events,
            session_start=session_start_utc(record),
            alignment=record.alignment,
        ),
    )


def _refresh_day_rules_file(root: Path, day: date) -> None:
    if load_current_day_json(root, day) is None:
        return
    pointer = _current_build_dir(root, day)
    if pointer is None:
        return
    path = pointer / DAY_RULES_NAME
    if not path.is_file():
        return
    try:
        body = store.read_json(path)
    except (OSError, ValueError):
        return
    if not isinstance(body, dict):
        return
    from tradevidanalyser.ledger import upsert_day_rule_checks
    from tradevidanalyser.schema import RuleCheck

    checks = []
    for item in body.get("rules") or []:
        if isinstance(item, dict) and item.get("rule"):
            checks.append(RuleCheck.model_validate(item))
    if checks:
        upsert_day_rule_checks(root, day, checks)


def _current_build_dir(root: Path, day: date) -> Path | None:
    pointer = day_dir(root, day) / "current"
    if not pointer.is_file():
        return None
    try:
        build_id = pointer.read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return None
    dest = day_dir(root, day) / "builds" / build_id
    return dest if dest.is_dir() else None


def _backup_rules(root: Path, session_id: str, dest: Path) -> None:
    src = store.rules_path(root, session_id)
    if not src.is_file():
        return
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest / src.name)


def _load_evidence(root: Path, session_id: str) -> Evidence | None:
    path = store.evidence_path(root, session_id)
    if not path.is_file():
        return None
    try:
        return Evidence.model_validate(store.read_json(path))
    except (OSError, TypeError, ValueError):
        return None


def _load_session_events(root: Path, session_id: str) -> list[SessionEvent]:
    path = store.insights_path(root, session_id)
    if not path.is_file():
        return []
    try:
        return list(store.load_insights(root, session_id).session_events)
    except (OSError, TypeError, ValueError):
        return []

