"""Build the synthetic L0 snapshot of ``main`` @ ``f44d864`` (plan §3.1)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import duckdb

from tradevidanalyser import config, store
from tradevidanalyser.ledger import ledger_db_path
from tradevidanalyser.ocr import OcrRow, write_ocr_parquet
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import (
    align_session,
    evidence_session,
    extract_session,
    fills_session,
    ledger_add,
    report_session,
    rules_session,
    transcribe_session,
)
from tradevidanalyser.publish import payload_from_debrief
from tradevidanalyser.schema import DebriefReport
from tests.conftest import make_test_video

FIXTURES = Path(__file__).parent / "fixtures"
L0_DIR = FIXTURES / "l0_main_f44d864"
SYNTHETIC = FIXTURES / "tradesviz_synthetic.csv"
VIDEO_NAME = "2026-05-14 16-03-00.mp4"
START = datetime(2026, 5, 14, 16, 3, tzinfo=timezone(timedelta(hours=2)))
OCR_OFFSET_S = 1.8


def _jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return value


def _dump_ledger(root: Path, session_id: str) -> dict:
    path = ledger_db_path(root)
    con = duckdb.connect(str(path), read_only=True)
    try:
        payload: dict[str, list] = {}
        for table in ("sessions", "trades", "rule_checks", "events"):
            rows = con.execute(
                f"SELECT * FROM {table} WHERE session_id = ? ORDER BY 1",
                [session_id],
            ).fetchall()
            cols = [col[0] for col in con.description]
            payload[table] = [_jsonable(dict(zip(cols, row, strict=True))) for row in rows]
    finally:
        con.close()
    return payload


def _clock_rows() -> list[OcrRow]:
    rows: list[OcrRow] = []
    for i in range(10):
        video_t = round(i * 0.2, 3)
        wall = START + timedelta(seconds=video_t + OCR_OFFSET_S)
        rows.append(
            OcrRow(
                t=video_t,
                roi="clock",
                text=wall.strftime("%H:%M:%S"),
                confidence=0.95,
                parsed=wall.isoformat(),
            )
        )
    return rows


def _run_variant(root: Path, video: Path, *, with_ocr: bool) -> Path:
    config.ensure_layout(root)
    record = ingest(video, root=root)
    transcribe_session(record.id, root=root)
    extract_session(record.id, root=root)
    if with_ocr:
        write_ocr_parquet(store.ocr_path(root, record.id), _clock_rows())
    align_session(record.id, root=root)
    fills_session(record.id, root=root, executions=SYNTHETIC, venue="amp")
    evidence_session(record.id, root=root)
    rules_session(record.id, root=root)
    report_session(record.id, root=root)
    ledger_add(record.id, root=root)
    return config.session_dir(root, record.id)


def _copy_variant(src_session: Path, dest: Path, root: Path, session_id: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in (
        "session.json",
        "fills.parquet",
        "trades.parquet",
        "evidence.json",
        "rules.json",
        "debrief.md",
        "debrief.json",
    ):
        shutil.copy2(src_session / name, dest / name)
    record = store.load_session(root, session_id)
    report = DebriefReport.model_validate(store.read_json(store.debrief_json_path(root, session_id)))
    payload = payload_from_debrief(record, report)
    (dest / "notion_payload.json").write_text(
        json.dumps(
            {
                "title": payload.title,
                "summaries": payload.summaries,
                "learnings": list(payload.learnings),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (dest / "ledger.json").write_text(
        json.dumps(_dump_ledger(root, session_id), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def generate_l0(dest: Path | None = None) -> Path:
    dest = dest or L0_DIR
    dest.mkdir(parents=True, exist_ok=True)
    inputs = dest / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    video = make_test_video(
        inputs / VIDEO_NAME,
        seconds=2.0,
        chapters=[(0.5, "open")],
    )
    shutil.copy2(SYNTHETIC, inputs / "executions.csv")

    from tempfile import TemporaryDirectory

    with TemporaryDirectory() as tmp:
        ocr_root = Path(tmp) / "ocr"
        fn_root = Path(tmp) / "filename"
        ocr_session = _run_variant(ocr_root, video, with_ocr=True)
        fn_session = _run_variant(fn_root, video, with_ocr=False)
        ocr_id = ocr_session.name
        fn_id = fn_session.name
        shutil.copy2(store.ocr_path(ocr_root, ocr_id), inputs / "ocr.parquet")
        _copy_variant(ocr_session, dest / "l0-ocr", ocr_root, ocr_id)
        _copy_variant(fn_session, dest / "l0-filename", fn_root, fn_id)
        assert not (ocr_root / "days").exists()
        assert not (fn_root / "days").exists()
    return dest


if __name__ == "__main__":
    generate_l0()
    print(L0_DIR)
