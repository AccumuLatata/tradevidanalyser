# TradeVidAnalyser (working name)

Headless, bot-first service that turns recorded futures trading sessions
(OBS screen + spoken commentary, **German**) into a time-stamped **Session
Record** and a small **HTTP API** the desk's Grok bots can query — without
anyone opening a video.

**Status: scoping.** No code yet. This repository holds the discovery, scope,
research, architecture and roadmap that precede implementation (milestone
TVA0). Series code **TVA**. Earlier drafts used the name *Debrief* / **DF**;
those labels are retired.

v1 does **not** join ThesisTester or import fills. It answers: *what was said
and seen on the tape?* Combining that with the lab (ThesisTester) and the
journal (TradesViz, AMP + TopstepX) is a later phase.

## Documents

| Doc | What it is |
|---|---|
| [`docs/01_DISCOVERY.md`](docs/01_DISCOVERY.md) | What exists on the desk today. Where the gap is. |
| [`docs/02_SCOPE.md`](docs/02_SCOPE.md) | Job, users, v1 vs later, principles, headless-API stance. |
| [`docs/03_STATE_OF_THE_ART.md`](docs/03_STATE_OF_THE_ART.md) | ASR, video LLMs, OCR, journals, Grok Bot — decision per layer. |
| [`docs/04_ARCHITECTURE.md`](docs/04_ARCHITECTURE.md) | Machines + NAS, pipeline, API/CLI, Session Record, later joins. |
| [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md) | TVA0–TVA3 (v1) and later phases; locked decisions D1–D9. |

## Planned shape (v1)

```
# local / scheduler (any of: trading PC, Mac, laptop — same NAS root)
tva ingest <video.mp4>
tva transcribe <session>
tva extract <session>
tva frames <session>          # optional visual notes / keyframes
tva run --latest

# always-on Mac serves the bots
tva serve --root /Volumes/nas/tradevid

# Grok bot
GET /sessions/latest
GET /sessions/{id}/insights
```

Python 3.11+, `pyproject.toml`, ruff + pytest, provider adapters with `fake`
implementations for CI. Facts stay in files on the NAS; the API is a read
surface, not a second source of truth.

## Running locally

Nothing to run yet. TVA0 records the locked decisions; TVA1 adds the skeleton
and `tva doctor`.
