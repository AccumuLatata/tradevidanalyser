# Debrief (working name)

Headless, bot-first pipeline that turns recorded futures trading sessions
(OBS screen + commentary) into a time-stamped **Session Record** aligned to
the actual fills, then into a **daily debrief** and a **coaching ledger** that
the desk's Grok bots consume. Companion to
[ThesisTester](https://github.com/AccumuLatata/ThesisTester) (the lab): the
lab answers *where* has edge, Debrief answers *how the trader behaves* against
his own plan and what it costs.

**Status: scoping.** No code yet. This repository currently holds the
discovery, scope, research, architecture and roadmap documents that precede
implementation (milestone DF0).

## Documents

| Doc | What it is |
|---|---|
| [`docs/01_DISCOVERY.md`](docs/01_DISCOVERY.md) | What exists on the desk today: bots, ThesisTester journal, TopstepX/TradesViz/OBS/Quantower, rulebook, Notion. Where the gap is. |
| [`docs/02_SCOPE.md`](docs/02_SCOPE.md) | Job, users (bots), in/out of scope, principles, rule catalog, success criteria, headless stance. |
| [`docs/03_STATE_OF_THE_ART.md`](docs/03_STATE_OF_THE_ART.md) | ASR, video LLMs, OCR, AI trade journals, Grok Bot, TopstepX API, OBS — and the decision per layer. |
| [`docs/04_ARCHITECTURE.md`](docs/04_ARCHITECTURE.md) | Topology (where it runs), pipeline stages and CLI, Session Record contract, ThesisTester relationship, bot routine pack. |
| [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md) | Milestones DF0–DF7 with exit criteria, risks, and the decisions needed to start. |

## Planned shape (for orientation)

```
debrief ingest <video.mp4>     # register session, extract audio tracks, chapters
debrief transcribe <session>   # word-timestamped ASR (WhisperX local by default)
debrief fills <session>        # TopstepX API (read-only) → trades + TradesViz/TJ CSV
debrief align <session>        # video clock ↔ fill clock, with confidence
debrief frames <session>       # keyframes, ROI OCR, per-trade clips
debrief extract <session>      # cited, structured evidence from speech
debrief rules <session>        # deterministic + evidence-backed rule checks
debrief context <session>      # day's brief, DRC, ThesisTester attribution
debrief report <session>       # debrief.md / debrief.json
debrief publish --notion       # Trading Journal page + one log line
debrief run --latest           # all of the above, resumable
```

Python 3.11+, `pyproject.toml`, ruff + pytest, provider adapters with fake
implementations for CI. Same engineering posture as ThesisTester: CLI is the
contract, no embedded agent, no MCP server, facts separated from
interpretation, never invent numbers.

## Running locally

There is nothing to run yet. DF0 adds the repo skeleton and `debrief doctor`.
