"""CLI contract: same stages the API exposes."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tradevidanalyser import __version__, config, store
from tradevidanalyser.doctor import run_doctor
from tradevidanalyser.ingest import ingest
from tradevidanalyser.pipeline import extract_session, run_latest, transcribe_session
from tradevidanalyser.serve import create_app
from tradevidanalyser.watch import watch
from tradevidanalyser.wer import score_session


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tva",
        description="TradeVidAnalyser — ingest OBS tapes, transcribe, serve insights.",
    )
    parser.add_argument("--root", type=Path, default=None, help="TVA_ROOT (NAS mount or local store)")
    parser.add_argument("--json", action="store_true", help="machine-readable stdout")
    parser.add_argument("--version", action="version", version=f"tva {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="register a recording and extract mic audio")
    p_ing.add_argument("video", type=Path)
    p_ing.add_argument(
        "--desktop-track",
        action="store_true",
        help="also extract 0:a:1 to audio/desktop.opus (never transcribed by default)",
    )

    p_tr = sub.add_parser("transcribe", help="ASR a registered session")
    p_tr.add_argument("session")
    p_tr.add_argument("--provider", default=None)

    p_ex = sub.add_parser("extract", help="cited insights from the transcript")
    p_ex.add_argument("session")
    p_ex.add_argument("--provider", default=None)

    sub.add_parser("status", help="list sessions and stage states")
    sub.add_parser("doctor", help="check ffmpeg, TVA_ROOT, GPU, keys")

    p_run = sub.add_parser("run", help="ingest (optional) + transcribe + extract")
    p_run.add_argument("--latest", action="store_true", help="use the newest session")
    p_run.add_argument("video", nargs="?", type=Path)
    p_run.add_argument(
        "--desktop-track",
        action="store_true",
        help="also extract 0:a:1 to audio/desktop.opus (never transcribed by default)",
    )

    p_watch = sub.add_parser("watch", help="copy finished OBS files into TVA_ROOT (record local)")
    p_watch.add_argument("--source", type=Path, required=True, help="local OBS output directory")
    p_watch.add_argument("--once", action="store_true", help="scan once and exit")
    p_watch.add_argument("--run", action="store_true", help="ingest + transcribe + extract after copy")
    p_watch.add_argument("--stable-seconds", type=float, default=None, help="mtime must be this old")
    p_watch.add_argument("--interval", type=float, default=None, help="poll interval when not --once")

    p_wer = sub.add_parser("wer", help="WER and jargon recall vs a reference transcript")
    p_wer.add_argument("session")
    p_wer.add_argument("--ref", type=Path, required=True, help="hand-corrected reference .txt")

    p_serve = sub.add_parser("serve", help="HTTP API over TVA_ROOT")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8764)

    args = parser.parse_args(argv)
    root = config.resolve_root(args.root)

    try:
        if args.cmd == "doctor":
            report = run_doctor(root)
            _emit(report.model_dump(mode="json"), as_json=args.json or True)
            return 0 if report.ok else 1
        if args.cmd == "ingest":
            record = ingest(args.video, root=root, desktop_track=args.desktop_track)
            _emit(record.model_dump(mode="json"), as_json=True)
            return 0
        if args.cmd == "transcribe":
            transcript = transcribe_session(args.session, root=root, provider_name=args.provider)
            _emit(transcript.model_dump(mode="json"), as_json=True)
            return 0
        if args.cmd == "extract":
            insights = extract_session(args.session, root=root, provider_name=args.provider)
            _emit(insights.model_dump(mode="json"), as_json=True)
            return 0
        if args.cmd == "status":
            ids = store.list_session_ids(root)
            rows = [store.compute_status(root, sid).model_dump(mode="json") for sid in ids]
            _emit({"root": str(root), "sessions": rows}, as_json=True)
            return 0
        if args.cmd == "run":
            video = args.video
            record = run_latest(root, video=video, desktop_track=args.desktop_track)
            status = store.compute_status(root, record.id)
            _emit(status.model_dump(mode="json"), as_json=True)
            return 0
        if args.cmd == "watch":
            def _on_events(events: list) -> None:
                interesting = [
                    event
                    for event in events
                    if event.get("action") != "skipped"
                    or event.get("reason")
                    not in {"ingested", "unstable", "busy", "locked"}
                ]
                if not interesting:
                    return
                _emit({"events": interesting}, as_json=True)
                sys.stdout.flush()

            try:
                payload = watch(
                    args.source,
                    root=root,
                    once=args.once,
                    run=args.run,
                    stable_s=args.stable_seconds,
                    poll_s=args.interval,
                    on_events=None if args.once else _on_events,
                )
            except KeyboardInterrupt:
                return 0
            _emit(payload, as_json=True)
            if any(event.get("action") == "error" for event in payload.get("events", [])):
                return 1
            return 0
        if args.cmd == "wer":
            _emit(score_session(args.session, ref=args.ref, root=root), as_json=True)
            return 0
        if args.cmd == "serve":
            import uvicorn

            app = create_app(root)
            uvicorn.run(app, host=args.host, port=args.port, log_level="info")
            return 0
    except (FileNotFoundError, ValueError, OSError, RuntimeError) as exc:
        _emit({"error": str(exc)}, as_json=True)
        return 1
    parser.error(f"unknown command {args.cmd}")
    return 2


def _emit(payload: dict, *, as_json: bool) -> None:
    if as_json:
        json.dump(payload, sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    print(payload)
