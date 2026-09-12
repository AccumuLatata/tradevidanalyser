# 05 — Engineering roadmap (DF0–DF7)

Same conventions as ThesisTester: numbered milestones, each additive, each
with an exit criterion that a bot or a human can check. No calendar estimates;
ordering and dependencies only. A milestone is "landed" when its exit
criterion holds on the golden session **and** on one real session.

| Milestone | Intent | Depends on | Status |
|---|---|---|---|
| **DF0** | Plan lock, decisions, desk prep (OBS, API key, host, sync path), golden session | — | this document |
| **DF1** | Ingest + transcript | DF0 | |
| **DF2** | Fills via TopstepX API + TradesViz/TJ-compatible CSV | DF0 | |
| **DF3** | Alignment + trade windows + evidence extraction + deterministic rules | DF1, DF2 | |
| **DF4** | Frames: ROI OCR, keyframes, clips, optional VLM notes | DF3 | |
| **DF5** | Context join (briefs, DRC, ThesisTester attribution) + debrief report + Notion publish | DF3 | |
| **DF6** | Ledger + weekly rollup + Debrief bot routine pack + Trade Importer retirement | DF5 | |
| **DF7** | Coach loop: trend questions, intent-tag proposals back to TradesViz/TJ, experiment tracking | DF6 | |
| later | Full-session Gemini agentic pass; live fill → OBS chapter stamping; read-only viewer; `journal propose-study` input | DF7 | parked |

---

## DF0 — Plan lock and desk preparation

Nothing to code except a repo skeleton. Everything here is a decision or a
setting; each one blocks a later milestone if skipped.

**Decisions Accumu must make (see §"Decisions needed" below):** D1 host,
D2 TopstepX API, D3 sync path, D4 daily loss value, D5 language, D6 providers.

**Desk settings (one-time):**
- OBS: Hybrid MP4; filename `%CCYY-%MM-%DD %hh-%mm-%ss`; mic on audio track 1,
  desktop/Grok audio on track 2; "Add chapter marker" hotkey bound; optional
  obs-websocket enabled for recording-stopped trigger. Confirm the platform
  clock is visible on screen for OCR calibration (note its screen region).
- TopstepX: ProjectX dashboard account, API subscription (promo `topstep`),
  link, generate key, store in the host's secret store.
- Record one **golden session** (20–30 min, practice account acceptable) with
  deliberate content: a spoken bias, a named playbook, one trade with stated
  stop/target, one re-entry, one hourly check-in, some German and some
  English. Export its fills. Hand-correct its transcript once (WER reference).

**Repo skeleton:** `pyproject.toml` (Python 3.11+, ruff, pytest), `debrief/`
package with empty stage modules, `docs/` (these files), `schemas/` (JSON
schema exports), CI running lint + tests. No providers wired yet.

**Exit:** decisions recorded in this file; golden session and fills stored at
the fixtures location; `debrief doctor` runs and reports what is missing.

## DF1 — Ingest and transcript

- `debrief ingest`: ffprobe metadata, wall-clock start from filename with
  creation-time cross-check, chapter list, per-track Opus extraction, hashes,
  `session.json`. Handles split files (OBS auto-split) as one session.
- `debrief transcribe`: provider interface + WhisperX adapter (local) + one
  hosted adapter; jargon `initial_prompt` from a checked-in glossary
  (`docs/GLOSSARY.md`: level tokens, playbooks, platform words); language per
  segment; word timestamps; confidences.
- WER harness against the hand-corrected golden transcript.

**Exit:** golden and one real session produce `transcript.json`; WER on the
golden ≤ 10 % overall and jargon terms ≥ 90 % recall; re-run is byte-identical
for `session.json` and stable (same model) for the transcript.

## DF2 — Fills

- Read-only TopstepX client: auth, token refresh, `Trade/search`,
  `History/retrieveBars`. **No order/position write methods exist in the
  module; a test asserts that.** Practice and live account ids configurable.
- Fallback loader for the TradesViz executions CSV (reuse ThesisTester
  profile) and for the TopstepX web export CSV.
- Pairing into round trips using ThesisTester's `JournalTrade` contract
  (import or mirror + contract test). Session date = trading session date
  (ETH start 18:00 ET), not Vienna calendar date.
- Emit `tradesviz_import.csv` in the format TradesViz accepts for TopstepX,
  and verify one upload by hand.

**Exit:** for three past sessions, fills from the API reconcile 1:1 with what
the Trade Importer uploaded (count, symbols, timestamps within 1 s, P&L);
TradesViz de-dupes the CSV as expected.

## DF3 — Alignment, evidence, rules

- `debrief align`: filename prior + OCR of the platform clock at K sampled
  frames (needs a minimal ROI OCR here; full frame work is DF4); offset, drift,
  confidence; fail-closed thresholds.
- Trade windows and session slots (hourly xx:50 ± 5 min).
- `debrief extract`: LLM structured extraction per window with citations
  enforced by schema; German/English; prompts versioned; `fake` provider for
  tests. Fields: stated setup / playbook / bias / stop / target, markers
  (tilt, hesitation, rule mention, brief ref, Grok ref), session events.
- `debrief rules`: the deterministic catalog (R-DLL, R-MAX10, R-3L30, R-5M,
  R-REENTRY, R-CLOSE) from fills only; the evidence-backed catalog with
  `unverifiable` when speech is absent or alignment is low.

**Exit:** on the golden session the planted facts (bias, playbook, stop,
target, re-entry, check-in) are all found with correct citations; on a real
session every deterministic rule returns pass/violated; alignment confidence
≥ 0.9.

## DF4 — Frames

- Keyframes at entry/exit ±(configurable) seconds and at chapter markers.
- ROI OCR (PaddleOCR) for clock, position, unrealised P&L, instrument; ROIs
  configured per layout in a YAML with a calibration helper that prints a
  contact sheet.
- Clips per trade (ffmpeg stream copy, mic track only, burned-in timestamp
  optional).
- Optional VLM notes per clip (Grok 4.3 `video_url` after verifying the
  official docs; Gemini as alternative), opt-in, cost-logged per session;
  redaction mask over account/balance regions before anything leaves the
  machine.

**Exit:** per-trade clips open at the right moment on a real session; OCR
P&L at exit matches the fill P&L on ≥ 90 % of trades; VLM notes, if enabled,
cite frame ids and never state a price the OCR did not read.

## DF5 — Context and debrief

- `debrief context`: Notion read of the day's Macro / NY brief (bias,
  conviction, kill levels, quoted); DRC scores if present; ThesisTester
  `journal attribute` / `triggers` results for the session's trades.
- `debrief report`: fixed page order (source strip · day in one paragraph ·
  trade table with evidence links · rule scorecard · brief-vs-behaviour ·
  observations · three candidate learnings · gaps); Markdown + JSON.
- `debrief publish --notion`: page in Trading Journal titled
  `<D Mon YYYY> Session Debrief`, tag *Trades Summary*, `Summaries` property
  = one sentence, `Learning 1–3` filled from candidates; update in place if
  it exists; one log line on a *Debrief runs* page.

**Exit:** a human can read the Notion page in under five minutes and check
any finding from the linked clip/quote; the page has no number that is not in
`fills`/`ocr`/`context`.

## DF6 — Ledger, rollup, bot routine

- `ledger.duckdb` with per-session and per-trade rows; `debrief rollup
  --week` produces a weekly Markdown (adherence rates, violation counts, time
  in market, trades per hour, stated-vs-attributed setup agreement).
- Routine pack (`docs/DEBRIEF_GROK_ROUTINE_PACK.md`): Debrief bot evening
  routine + watchdog, hard rules, Sunday additions, Trade Importer change.
- Run in shadow for two weeks alongside the Trade Importer; then flip the
  importer to fallback-only.

**Exit:** ten consecutive weekday sessions processed by the bot without manual
intervention; Trade Importer's browser export not needed on any of them.

## DF7 — Coach loop

- Intent-tag proposals from speech written as TradesViz notes/tags CSV and
  `intent_proposals.json`; human confirmation status tracked.
- Coach prompt over the ledger: trend questions with citations, one weekly
  behavioural experiment as a testable rule (e.g. "no entries in the first
  15 min after a loss for two weeks"), tracked in the ledger with its outcome.
- Edge Finder hook: per-trade evidence + lab attribution side by side.

**Exit:** the weekly review contains at least one claim that is backed by
≥ 10 cited instances across sessions, and one experiment is running with a
defined stop criterion.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Clock misalignment (OBS start vs exchange time, pauses, splits) | evidence attached to the wrong trade | OCR'd platform clock calibration, chapter markers, fail-closed `unverifiable` |
| ASR misses jargon or German/English switches | wrong or missing intent | jargon prompt/glossary, WER harness, hosted fallback for comparison |
| LLM invents a quote or number | destroys trust | schema-enforced citations, drop uncited claims, numbers only from fills/OCR |
| Videos or frames with account data leave the desk | PII exposure | T1 topology, redaction masks, per-provider opt-in, hosted calls get audio + crops only |
| TopstepX API key misuse | live account risk | client has no order methods (tested), key in secret store, read-only server allowed by ToS |
| Trading PC busy post-session | pipeline delayed | run after session end at low priority; T3 degraded mode |
| Provider churn (models, prices, endpoints) | rework | adapter interfaces, pinned versions, `fake` providers in CI |
| Scope creep into a UI or into ThesisTester's territory | dilution | out-of-scope list in `02_SCOPE.md`; contract tests instead of re-implementation |
| Two conflicting daily-loss values ($100 vs $200) | wrong violations | D4 decision; rule catalog reads one config value |

---

## Decisions needed from Accumu (block DF0)

| # | Decision | Options | Default if silent |
|---|---|---|---|
| D1 | Processing host | trading PC · Mac (Program B box) · bot cloud computer | trading PC (T1) |
| D2 | TopstepX API subscription ($14.50/mo) | yes · no (keep browser export as source) | yes |
| D3 | Artifact sync path PC → bot | Google Drive on `tradingautomations1` · S3/B2 bucket · git-LFS | Google Drive (already signed in on the bot box) |
| D4 | Daily loss limit for R-DLL | $100 (DRC) · $200 (Plan) | — must be chosen |
| D5 | Debrief language | English page + German chat ping (current bot convention) · German page | English page, German ping |
| D6 | Model providers | ASR: WhisperX local / hosted; extraction: xAI Grok; VLM: Grok 4.3 / Gemini | as listed |
| D7 | Is Grok used by voice during sessions (desktop audio contains Grok)? | yes → separate OBS track, transcribe both · no | separate track anyway |
| D8 | Practice vs live account for the first weeks of fills | practice · live | live (read-only) |
| D9 | Where the ThesisTester journal runs today (which machine has the store) | PC · Mac · bot | — needed for DF5 |

---

## Non-goals restated (so the bots do not drift)

Do not build a UI. Do not compute levels. Do not rank setups by outcome from a
few sessions. Do not let the coach edit facts. Do not add order endpoints. Do
not upload raw video by default. Do not write to Notion columns the schema
does not have.
