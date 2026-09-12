# TradeVidAnalyser — Implementation Plan (TVA)

**Document type:** Focused implementation plan (fully scoped PRs)
**Date:** 2026-09-12 (rev 1)
**Status:** **TVA0 locked.** PR-01 (skeleton) landed on `main`.
**Series prefix:** **TVA**. Not DF (retired), not TJ/JS (ThesisTester).
**Reader:** desk owner, the Grok bot that will implement PRs, engine contributors.
**Companion docs:** [`02_SCOPE.md`](02_SCOPE.md) (what), [`04_ARCHITECTURE.md`](04_ARCHITECTURE.md)
(shape), [`05_ROADMAP.md`](05_ROADMAP.md) (locked D1–D9), [`GLOSSARY.md`](GLOSSARY.md).

This file is the **build contract**. If a PR is not listed here, it is not in
scope until this file is amended. Every PR is additive, ships tests, and
touches the docs listed in §7.

---

## 0. Finding (locked — verified 2026-09-12)

### 0.1 Statement

The desk records every NY session with OBS (screen + German mic commentary)
and nothing consumes those tapes. Fills already reach TradesViz (TopstepX
today, AMP as the destination). ThesisTester already owns level attribution,
trigger inference and counterfactuals over a TradesViz executions export.
The gap is **evidence from the tape**, delivered to Grok without a human
opening a video.

### 0.2 What exists (do not rebuild)

| Surface | Owner | Reuse how |
|---|---|---|
| Fill parsing (`tradesviz_executions` profile → `FillRecord`) | ThesisTester `journal/tradesviz.py` | TVA4 imports or contract-tests against it |
| Pairing (`spread_id` + FIFO → `JournalTrade`) | ThesisTester `journal/pair.py::pair_journal_trades` | TVA4 calls it |
| AMP money-truth (`AmpStatement`) | ThesisTester `journal/amp_statement.py` | TVA4 optional input; never re-parsed here |
| Level tokens / zones / triggers per entry | ThesisTester `journal attribute\|zones\|triggers` | TVA6 reads their parquet output |
| Briefs (English), DRC, Trading Journal DB | Notion, existing bots | TVA6 reads pages; publishing stays with the bot |
| Bot conventions (quiet on success, one log line, never invent numbers) | Grok Bot Routines page | TVA2 routine pack mirrors them |

### 0.3 What was verified in the skeleton (PR-01)

- OBS `%CCYY-%MM-%DD %hh-%mm-%ss` → `YYYY-MM-DD_HHMMSS` session id; two
  sessions on one day do not collide.
- `ffprobe` chapters/streams/duration are readable from Hybrid MP4.
- `ffmpeg -map 0:a:0 -c:a libopus -b:a 32k -ac 1` yields ~14 MB per audio-hour.
- Pydantic v2 Session Record round-trips byte-identically on re-ingest.
- Citation guard: every `CitedSpan.text` must be a substring of its segment.
- FastAPI over the store, `GET /sessions/latest`, works from tests.

### 0.4 Locked desk facts that shape contracts

| Fact | Consequence |
|---|---|
| Videos are German; briefs are English (D5) | ASR `language="de"`; insights store German verbatim; extractor prompt is German-aware; English tokens (ONH, dVWAP, 3c) are jargon, not language switches |
| AMP is the goal, TopstepX is a current venue (D2) | No broker API in the spine. `venue` is a column on fills. TradesViz executions CSV is the only fill loader in this series |
| Three machines + Synology NAS (D1, D3, D9) | `TVA_ROOT` is the only location config. Record local, copy after. API host = Mac |
| Grok bot is off-LAN | HTTP API is the bot surface; Tailscale/tunnel is wiring, not code |
| No Grok on the tape (D7) | No diarization. Mic = `0:a:0`. Desktop track optional |
| Real live-account tapes (D8) | Redaction masks before any frame leaves the machine; fixtures are redacted excerpts on the NAS, not in git |
| Daily loss $100 vs $200 unresolved (D4) | `rules.yaml` config value; decided at TVA5, not hard-coded |

### 0.5 What this series does not claim

- It does not compute levels, edge, or expectancy. ThesisTester does.
- It does not replace TradesViz statistics or the Trade Importer.
- It does not grade a trade as good/bad from outcome. It grades **process
  vs plan** and records **what was said**.
- Insights are model output; every one carries a citation or is dropped.

---

## 1. Value thesis, goals, non-goals

### 1.1 Four decisions the product changes

1. **Did I follow my plan today?** (rule scorecard, TVA5)
2. **What did I say I was doing vs what the lab says I did?** (TVA6 join)
3. **Which behaviours repeat across weeks?** (ledger + coach, TVA6/TVA7)
4. **Where is the 60-second clip that proves it?** (evidence, TVA3/TVA5)

Before any of those: **can Grok read the tape at all?** (TVA1–TVA2). If a
milestone does not move one of these, do not build it.

### 1.2 Goals

- G1 A finished OBS recording becomes `transcript.json` + `insights.json`
  under `TVA_ROOT` with no human input, on the trading PC or the Mac.
- G2 A Grok bot can `GET /sessions/latest` from off-LAN and produce a
  session note Accumu can check against the tape in under five minutes.
- G3 Same package, same `TVA_ROOT` layout, runs on Windows (PC, laptop) and
  macOS.
- G4 Every quote cites a segment; every number is a fill/OCR read or null.
- G5 CI never calls a paid API or needs a GPU.
- G6 Later: fills from TradesViz, alignment ±2 s, rule scorecard, lab join,
  ledger, coach loop — each additive, each opt-in.

### 1.3 Non-goals (entire series)

- GUI of any kind. `/docs` from FastAPI is fine; no pages.
- Order transmission to any broker. No broker write client exists.
- Real-time in-session coaching.
- Full-frame "watch the chart" understanding as a required stage.
- Emotion recognition from face/webcam.
- A TopstepX- or AMP-specific spine.
- A second component library, queue, MCP server, or embedded agent.

---

## 2. Architecture (as built + planned)

```
src/tradevidanalyser/
  __init__.py  __main__.py  cli.py  config.py  schema.py  store.py
  naming.py    media.py     ingest.py  doctor.py  pipeline.py  serve.py
  providers/   asr.py (fake, whisperx*) · extract.py (fake, grok*)
  # planned
  providers/asr_hosted.py     TVA1  hosted fallback (Deepgram or Scribe)
  glossary.py                 TVA1  GLOSSARY.md → initial_prompt / token list
  wer.py                      TVA1  WER + jargon recall harness
  watch.py                    TVA1  recording-finished detector + copy
  frames.py  ocr.py  clips.py TVA3  keyframes / ROI OCR / chapter clips
  redact.py                   TVA3  mask ROIs before any frame leaves
  providers/vlm.py            TVA3  opt-in visual notes (fake, grok, gemini)
  fills.py                    TVA4  TradesViz executions → fills/trades parquet
  align.py                    TVA5  video ↔ wall clock ↔ fill clock
  evidence.py                 TVA5  per-trade windows + cited evidence
  rules.py  rules.yaml        TVA5  deterministic + evidence-backed catalog
  context.py                  TVA6  Notion briefs/DRC + ThesisTester parquet
  report.py                   TVA6  debrief.md / debrief.json
  ledger.py                   TVA6  DuckDB per-session/per-trade rows
  proposals.py                TVA7  intent tags → TradesViz CSV
  coach.py                    TVA7  cited trend questions + experiments
tests/                        fixtures are synthetic; real excerpt lives on NAS
docs/                         this plan, scope, architecture, glossary, routine pack
```

**Stage order per session** (each idempotent, each writes one file):

```
ingest → transcribe → extract → [frames] → [fills → align → evidence → rules] → [context → report → ledger] → [proposals]
```

Bracketed stages are later milestones and skip cleanly when their inputs
are absent (`status.stages[x] = "missing"`, never an exception on read).

**Store layout** (`TVA_ROOT`):

```
recordings/<original OBS filename>
sessions/<YYYY-MM-DD_HHMMSS>/
  session.json  status.json  audio/mic.opus  transcript.json  insights.json
  frames/  ocr.parquet  clips/                 (TVA3)
  fills.parquet  trades.parquet  alignment.json  evidence.json  rules.json   (TVA4–5)
  context.json  debrief.md  debrief.json         (TVA6)
  intent_proposals.json  tradesviz_tags.csv     (TVA7)
ledger/ledger.duckdb                            (TVA6)
fixtures/golden/<excerpt>/ …                    (NAS only; never in git)
```

---

## 3. Locked contracts

### 3.0 Rules that apply to every PR

- **Schema-versioned files.** Every JSON has `schema_version`. Readers
  tolerate missing later-stage files.
- **Facts vs model.** Files in the "facts" set (`session.json`, `fills`,
  `trades`, `alignment`, `ocr`, `rules`) are byte-identical on re-run given
  the same inputs. Model files carry `provider`, `model`, `prompt_version`.
- **Citations or nothing.** A model-derived span must cite a `seg` id (and
  optionally frame ids) that exist; its `text` must be a substring of that
  segment. The guard in `pipeline._assert_citations` is the reference
  implementation and must be extended, never bypassed.
- **Numbers.** Prices, P&L, counts appear only from fills or OCR reads (with
  confidence). Spoken numbers stay `raw_text`.
- **Providers.** Every external model sits behind a Protocol with a `fake`
  implementation used by default and in CI. Selection is an env var
  (`TVA_ASR_PROVIDER`, `TVA_EXTRACT_PROVIDER`, `TVA_VLM_PROVIDER`).
- **PII.** Raw video and full frames never leave `TVA_ROOT`. Hosted calls
  receive audio, redacted crops, or text only, and only when the provider
  is explicitly selected.
- **No broker client.** No module may import or implement an order
  endpoint. A test greps `src/` for `placeOrder|/api/Order|cancelOrder`.
- **Windows + macOS.** Paths via `pathlib`; no shell pipes; ffmpeg via
  `subprocess` list args; tests skip GPU.
- **CLI = API.** Every stage is callable from `tva <stage>` and from
  `POST /sessions/{id}/run?stages=…`. Same function, same file output.

### 3.1 Session Record v1 (`session.json`) — landed

```yaml
schema_version: "1"
id: 2026-09-11_143000
recording: {path, sha256, start_wallclock_vienna, duration_s, tracks, chapters: [{t, name}], filename}
language: de
alignment: null          # TVA5 fills {offset_s, drift_s_per_h, confidence, method}
app_version: 0.1.0
```

Additive fields allowed; renames are a schema bump.

### 3.2 Transcript (`transcript.json`) — landed

Segments with `id`, `t0`, `t1`, `lang`, `text`, `words[{w,t0,t1,p}]`.
Segment ids are `seg_NNN` zero-padded, stable for a given provider+model.
`prompt_version` names the glossary revision used.

### 3.3 Insights (`insights.json`) — landed, extended in TVA2

Fields: `bias_statements`, `playbooks_mentioned`, `stated_levels`,
`stated_stops_targets`, `checkins`, `tilt_markers`, `brief_refs`,
`observations`, `gaps`. Each entry is a `CitedSpan{seg, text, t, name?,
token?, raw_text?}`. TVA2 adds `session_events[{t, kind, seg, text}]` with
`kind ∈ {hourly_checkin, bias_statement, no_trade_zone, trade_zone, tilt,
break, rule_mention, brief_ref, grok_ref}` and `summary_de` / `summary_en`
(model prose, labelled). Nothing else is prose.

### 3.4 Fills and trades (TVA4)

`fills.parquet` = ThesisTester `FILL_RECORD_COLUMNS` exactly
(`fill_id, source, source_group_id, instrument, contract_month,
contract_year, side, qty, price, timestamp, session_date, entry_kind, tags,
notes_text, declared_stop, declared_target, flags`) **plus** additive
`venue` (`topstepx | amp | unknown`, derived from a `--venue` flag or a
filename hint; never guessed from prices).

`trades.parquet` = ThesisTester `JOURNAL_TRADE_COLUMNS` exactly, plus
additive `venue`, `tva_trade_id` (`T01…`, ordered by `entry_timestamp`).

Source of both: `thesistester.journal.tradesviz.load_tradesviz_executions(
path, profile="tradesviz_executions")` and
`thesistester.journal.pair.pair_journal_trades(...)` when the
`tradevidanalyser[journal]` extra is installed; otherwise a **mirror**
loader that is contract-tested against a committed synthetic CSV and its
expected parquet produced once by the ThesisTester function. Mirror and
import must produce identical frames (test).

Clock: TradesViz timestamps are UTC with explicit offset. `session_date`
is ThesisTester's trading session date (ETH 18:00 ET). Video wall-clock is
Europe/Vienna. The join happens on UTC instants, never on calendar dates.

### 3.5 Alignment (TVA5)

```yaml
alignment:
  offset_s: 1.8          # wall_clock = video_t + start_wallclock + offset
  drift_s_per_h: 0.0
  confidence: 0.96       # 0–1
  method: ocr_clock | filename | chapter_fill | manual
  samples: [{video_t, ocr_text, parsed_wallclock, residual_s}]
```

Fail-closed thresholds: `confidence < 0.8` → every timing-dependent rule is
`unverifiable`; evidence windows still emitted with `alignment: low`.
Target ±2 s.

### 3.6 Evidence (TVA5)

Per `tva_trade_id`: `window{t0,t1}` (entry −180 s … exit +120 s in video
time, configurable), `commentary[seg ids]`, `stated{setup, bias, stop_raw,
target_raw, playbook}` each `{value, seg} | null`, `markers[]`, `frames[]`,
`ocr[]`, `clip`, `alignment_confidence`.

### 3.7 Rules (TVA5)

`rules.json`: `[{rule, status: pass|violated|unverifiable, evidence{…},
reason?}]`. Catalog and thresholds live in `rules.yaml` (checked in) with
`daily_loss_limit_usd` **unset by default** → R-DLL `unverifiable` until
the desk sets it (D4). Deterministic rules never consult the model.

### 3.8 Context, debrief, ledger (TVA6)

`context.json`: `brief{macro_url, ny_url, bias_nq, bias_es, conviction,
kill_levels, quoted: true}`, `drc{scores, url} | null`,
`lab{per_trade: {tva_trade_id: {nearest_level_token, level_context,
tag_alignment, inferred_triggers_1m, zone_id}}} | null` — read from
ThesisTester attribution/zones/triggers parquet by `trade_id` ↔
`entry_fill_id` mapping, never recomputed.

`debrief.md` page order is fixed: source strip · day in one paragraph ·
trade table with evidence links · rule scorecard · brief-vs-behaviour ·
observations · three candidate learnings · gaps. `debrief.json` mirrors
it with ids. Ledger tables: `sessions`, `trades`, `rule_checks`,
`events`; DuckDB reads the Parquet files directly.

### 3.9 Proposals and coach (TVA7)

`intent_proposals.json`: `[{tva_trade_id, proposed_tags[], source_segs[],
status: proposed|confirmed|rejected}]`. `tradesviz_tags.csv` matches the
TradesViz manual-import column set (`date, symbol, side, price, quantity,
tags, notes`) with `notes` prefixed `[TVA proposed]`. Coach output is a
Markdown with ≥ N cited instances per claim (`N` from config, default 10)
and at most one experiment with a stop criterion.

### 3.10 PII and fixtures

- Git holds **synthetic** media only (ffmpeg `sine` + `color`, generated
  in tests) and synthetic CSV/JSON.
- The **golden excerpt** (20–30 min real German commentary, redacted) lives
  at `TVA_ROOT/fixtures/golden/`. Tests that need it are marked
  `@pytest.mark.golden` and skip when the path is absent.
- Redaction masks (`redact.py`) are applied before any frame is written to
  `frames/` or sent to a provider. Mask regions are in `layout.yaml`.

---

## 4. Milestone and PR table

| Milestone | PR | Intent | Depends on | Status |
|---|---|---|---|---|
| **TVA0** | PR-00 | Plan lock (docs 01–05, D1–D9) | — | landed |
| **TVA0** | PR-01 | Skeleton: package, CLI, store, fake providers, API, tests, CI | PR-00 | landed |
| **TVA0** | PR-02 | This plan + routine-pack stub + `layout.yaml` skeleton + golden-excerpt recipe | PR-01 | this PR |
| **TVA1** | PR-03 | Glossary loader + WER/jargon harness + `@golden` markers | PR-02 | |
| **TVA1** | PR-04 | WhisperX adapter (local GPU) + CPU fallback path | PR-03 | |
| **TVA1** | PR-05 | Hosted ASR adapter (Deepgram or Scribe v2) behind same Protocol | PR-03 | |
| **TVA1** | PR-06 | `tva watch` + copy-after-record + lock file + Windows Task / launchd recipes | PR-01 | |
| **TVA1** | PR-07 | OBS split-file stitching + desktop-track opt-in | PR-04 | |
| **TVA2** | PR-08 | Grok extractor (constrained JSON, German prompt, citations) + prompt versioning | PR-03 | |
| **TVA2** | PR-09 | `session_events` + `summary_de/en` + extended citation guard | PR-08 | |
| **TVA2** | PR-10 | API hardening: `?stages=`, `days=`, auth token, bind checks, `/openapi` examples, contract tests | PR-01 | |
| **TVA2** | PR-11 | `docs/TVA_GROK_ROUTINE_PACK.md` + `examples/bot/` prompts + Sunday audit lines | PR-10 | |
| **TVA3** | PR-12 | `layout.yaml` + keyframes at chapters + contact sheet | PR-07 | |
| **TVA3** | PR-13 | ROI OCR (PaddleOCR) for platform clock, position, P&L + `ocr.parquet` | PR-12 | |
| **TVA3** | PR-14 | `redact.py` masks + chapter clips (stream copy, mic only) | PR-12 | |
| **TVA3** | PR-15 | Opt-in VLM notes provider (fake, Grok 4.3, Gemini) + cost log | PR-14 | |
| **TVA4** | PR-16 | `fills.py`: TradesViz executions → `fills.parquet`/`trades.parquet` (import or mirror) + `venue` | PR-02 | later |
| **TVA4** | PR-17 | Optional AMP statement recon passthrough via ThesisTester `reconcile` output | PR-16 | later |
| **TVA5** | PR-18 | `align.py`: filename prior + OCR clock fit + confidence + thresholds | PR-13, PR-16 | later |
| **TVA5** | PR-19 | `evidence.py`: trade windows, cited stated fields, frames/ocr/clip refs | PR-18 | later |
| **TVA5** | PR-20 | `rules.py` deterministic catalog (R-DLL…R-CLOSE) + `rules.yaml` + D4 config | PR-16 | later |
| **TVA5** | PR-21 | Evidence-backed rules (R-PLAYBOOK…R-TILT) with `unverifiable` semantics | PR-19, PR-20 | later |
| **TVA6** | PR-22 | `context.py`: Notion briefs/DRC read + ThesisTester parquet join | PR-19 | later |
| **TVA6** | PR-23 | `report.py`: `debrief.md/json` fixed order + Grok prose provider | PR-21, PR-22 | later |
| **TVA6** | PR-24 | `ledger.py` DuckDB + `tva rollup --week` | PR-23 | later |
| **TVA6** | PR-25 | Optional `tva publish --notion` (Trading Journal page + one log line) | PR-23 | later |
| **TVA7** | PR-26 | `proposals.py`: spoken setups → TradesViz tag CSV + status tracking | PR-22 | later |
| **TVA7** | PR-27 | `coach.py`: cited trend questions + one experiment with stop criterion | PR-24 | later |
| parked | — | Full-session Gemini agentic pass; live fill → OBS chapters (TopstepX SignalR, read-only); read-only viewer; TopstepX API venue adapter | — | parked |

Ordering: PR-03 → PR-04/05 in parallel. PR-06 independent. PR-08 needs
PR-03 only (works on fake transcripts). PR-10/11 can land any time after
PR-01. TVA3 needs PR-07 for stable frame timing. TVA4 has no dependency on
TVA3. TVA5 needs both. TVA6 last of the join. TVA7 after the ledger exists.

---

## 5. Per-PR scope and acceptance

Each PR lists: files, behaviour, tests, docs, exit. "Exit" is checkable by
a bot from the CLI or the test suite. Nothing here requires a UI.

### PR-02 — Plan lock + stubs (this PR)

**Files:** `docs/IMPLEMENTATION_PLAN.md` (this), `docs/TVA_GROK_ROUTINE_PACK.md`
(stub: surfaces, hard rules, "not yet" table), `layout.yaml` (empty ROI
list with schema comment), `docs/GOLDEN_EXCERPT.md` (how to record, redact
with ffmpeg crop/box, hand-correct, where to put it).
**Tests:** none beyond existing suite green.
**Exit:** `ruff check` + `pytest -q` green; roadmap status row updated.

### PR-03 — Glossary + WER harness

**Files:** `glossary.py` (parse `GLOSSARY.md` sections → `initial_prompt`
string ≤ 220 tokens, `level_tokens` tuple, `playbook_terms`), `wer.py`
(word error rate, normalised German: casing, punctuation, umlaut folding
off by default; `jargon_recall(ref, hyp, tokens)`), `tva wer <session>
--ref <txt>` CLI, `@pytest.mark.golden` marker in `pyproject.toml`.
**Behaviour:** `FakeExtractProvider` and future ASR read tokens from the
glossary, not from a hard-coded tuple.
**Tests:** glossary parse is deterministic; WER on identical text = 0; on a
known edit = expected; jargon recall counts `ONH` inside German sentence.
**Docs:** `GLOSSARY.md` gets a "how this is used" header.
**Exit:** `tva wer` prints `{wer, jargon_recall, n_words}`; golden test
skips cleanly when the excerpt is absent.

### PR-04 — WhisperX adapter

**Files:** `providers/asr.py::WhisperXAsrProvider` (real), optional extra
`tradevidanalyser[whisperx]` = `whisperx`, `torch` (CUDA on PC, CPU on Mac
with a warning), `doctor` check `whisperx` importable + CUDA availability.
**Behaviour:** `language="de"`, `initial_prompt` from glossary, word
alignment on, batch size from env, `compute_type` int8 on CPU / float16 on
CUDA. Output → `Transcript` with `seg_NNN` ids, per-segment `lang` (Whisper
detected language per segment; default `de`). Model + version pinned in
`prompt_version`/`model`. No diarization.
**Tests:** unit test with a stub `whisperx` module injected via
`monkeypatch` (CI has no model); `@golden` test asserts WER ≤ 0.10 and
jargon recall ≥ 0.90 on the excerpt.
**Docs:** README install matrix (PC CUDA / Mac CPU / hosted).
**Exit:** on the trading PC, a real 4-hour session transcribes in under
30 min wall time; `transcript.json` re-run with the same model is
identical modulo floating word times (test tolerance 20 ms).

### PR-05 — Hosted ASR adapter

**Files:** `providers/asr_hosted.py` (`DeepgramAsrProvider` or
`ScribeAsrProvider`; pick one, keep the interface), env `TVA_ASR_PROVIDER=
deepgram|scribe`, key `DEEPGRAM_API_KEY` / `ELEVENLABS_API_KEY`, `doctor`
check.
**Behaviour:** uploads `audio/mic.opus` only; language `de`; keyword
boost from glossary where the API supports it; response mapped to the
same `Transcript`; cost estimate logged to `status.json.cost_usd`.
**Tests:** HTTP mocked with `httpx.MockTransport`; response fixture
committed; mapping test; "never called when provider is fake" test.
**Exit:** Mac without GPU produces a transcript for a real session; the
PII rule holds (audio only, no frames).

### PR-06 — `tva watch` and copy-after-record

**Files:** `watch.py` (poll a source dir for `*.mp4|*.mkv` whose mtime is
stable for N seconds and not open; copy to `TVA_ROOT/recordings/` with
`.part` then rename; write `recordings/<name>.lock` while copying; call
`ingest` then optional `run`), `tva watch --source <obs dir> [--once]
[--run]`, `scripts/windows/tva-watch.xml` (Task Scheduler), `scripts/mac/
com.tva.watch.plist` (launchd), `scripts/windows/copy-after-obs.ps1`.
**Behaviour:** never records to the NAS; never deletes the source; skips
files whose id is already ingested with the same sha256.
**Tests:** temp dirs; a growing file is not picked up; a stable file is
copied once; lock is removed; re-run is a no-op.
**Docs:** `04_ARCHITECTURE.md` §2.1 rules link to the scripts.
**Exit:** on the PC, stopping OBS results in a registered session on the
NAS within `N+copy` seconds with no manual step.

### PR-07 — Split-file stitching + desktop track

**Files:** `ingest.py` (detect OBS auto-split siblings: same prefix,
consecutive timestamps within `gap ≤ 5 s` of previous `duration`; treat as
one session with `recording.parts[]` and cumulative `t` offsets),
`media.extract_track(src, index, dest)`, `--desktop-track` flag writes
`audio/desktop.opus` (never transcribed by default).
**Tests:** two synthetic parts → one session; chapter times shifted by
part offset; single file unchanged.
**Exit:** a split real session yields one `session.json` with two parts
and continuous transcript times.

### PR-08 — Grok extractor

**Files:** `providers/extract.py::GrokExtractProvider` (real), `prompts/
insights_v1.de.md` (German system prompt; explains fields; forbids
numbers; demands `seg` ids), `prompts/README.md`, env `XAI_API_KEY`,
`TVA_EXTRACT_MODEL` (default a current Grok text model), JSON-schema
constrained output from the Pydantic `Insights` model.
**Behaviour:** windows of ≤ 40 segments with overlap 5; each window call
returns partial `Insights`; merge de-duplicates by `(seg, field)`; the
citation guard runs after merge and **drops** offending spans into
`gaps[]` with a reason rather than failing the stage. `prompt_version`
= file hash prefix.
**Tests:** `httpx.MockTransport` fixture responses (one valid, one with a
fabricated quote → dropped, one with an unknown seg → dropped); merge is
order-independent; no call when provider is fake.
**Docs:** `03_STATE_OF_THE_ART.md` §7 decision confirmed; prompt file
documented.
**Exit:** golden excerpt yields the planted bias, playbook, stop/target
and check-in with correct segment citations (`@golden` test).

### PR-09 — Session events + summaries

**Files:** `schema.py` (`SessionEvent`, `summary_de`, `summary_en`),
`extract.py` second pass (events over the whole timeline in one call with
segment ids only, no text, then re-hydrate text from transcript), citation
guard covers events and rejects summaries containing digits that are not
present in any cited segment (numbers rule).
**Tests:** digit-leak test; event kinds closed set; schema bump not
required (additive).
**Exit:** `insights.session_events` non-empty on the golden excerpt;
`summary_de` ≤ 120 words; no uncited digits.

### PR-10 — API hardening

**Files:** `serve.py` (`GET /sessions?days=7&status=…`, `POST
/sessions/{id}/run?stages=transcribe,extract` running in a background
thread with `status.stages[x]="running"`, `GET /sessions/{id}/status`,
optional `GET /sessions/{id}/clips/{name}` gated by `TVA_SERVE_MEDIA=1`),
bearer token via `TVA_API_TOKEN` (required when host is not loopback),
refuse to bind `0.0.0.0` without token, `examples/api/*.json` committed
responses, OpenAPI examples from the Pydantic models.
**Tests:** token required off-loopback; media 404 when gated; `days`
filter; background run flips status; contract test compares `/sessions/
latest` shape to `examples/api/latest.json` keys.
**Docs:** README API section; `04_ARCHITECTURE.md` §6 reachability.
**Exit:** from another machine on the LAN, `curl -H "Authorization:
Bearer …" http://mac:8764/sessions/latest` works; without token 401.

### PR-11 — Grok routine pack

**Files:** `docs/TVA_GROK_ROUTINE_PACK.md` (full), `examples/bot/
SYSTEM.md`, `examples/bot/ROUTINE_EVENING.md`, `examples/bot/
ROUTINE_SUNDAY_AUDIT.md`.
**Content:** surfaces (API only), evening routine (health → latest →
if insights present write the bot's own Notion note → one log line →
ping only on `failed|missing`), 23:30 watchdog, hard rules (never invent
a quote; every quote must exist in `/transcript`; never treat a spoken
price as a fill; never call anything but the API; never store LAN IPs;
token is a secret), Sunday audit additions, "not yet" table listing
TVA3–TVA7 outputs as absent.
**Exit:** a fresh Grok bot given only this pack completes the evening
routine against a real session.

### PR-12 — Layout + keyframes

**Files:** `layout.yaml` (named ROIs in normalised coordinates per
`layout_id`; `clock`, `position`, `pnl`, `instrument`, `account_mask`,
`balance_mask`), `frames.py` (`extract_frames(session, times) →
frames/<t>.jpg` via ffmpeg `-ss` accurate seek; default times = chapter
markers ± 0/2/5 s), `tva frames <session> [--at t…] [--contact-sheet]`.
**Tests:** synthetic video with a burned-in `drawtext` timestamp;
extracted frame count; contact sheet exists.
**Exit:** a chapter frame from a real session shows the moment the hotkey
was pressed.

### PR-13 — ROI OCR

**Files:** `ocr.py` (`PaddleOCR` behind `OcrProvider` Protocol with
`fake`), extra `tradevidanalyser[ocr]`, `ocr.parquet` columns `t, roi,
text, confidence, parsed` (`parsed` = ISO time for `clock`, signed float
for `pnl`, int for `position`), `tva ocr <session>`.
**Tests:** synthetic frames with `drawtext` known strings → parsed; fake
provider round trip; low-confidence rows kept with `parsed=null`.
**Exit:** OCR'd clock on the golden excerpt is within 1 s of the filename
prior at ≥ 90 % of samples.

### PR-14 — Redaction + clips

**Files:** `redact.py` (apply `*_mask` ROIs as black boxes before saving
frames; also applied to clips via ffmpeg `drawbox` when `--redact`),
`clips.py` (`ffmpeg -ss -to -c copy` with mic track only; default window
chapter −30 s … +60 s), `tva clips <session>`.
**Tests:** pixel test that mask region is black; clip duration; audio
stream count = 1.
**Exit:** no frame under `frames/` shows the masked regions (visual spot
check + pixel test on synthetic).

### PR-15 — Opt-in VLM notes

**Files:** `providers/vlm.py` (`VlmProvider` Protocol; `fake`, `grok`
(`video_url` per current xAI docs — **verify the doc page first**, spike
noted in `03_STATE_OF_THE_ART.md`), `gemini` (File API)), `visual_notes`
field on evidence/insights with `frames_cited[]`, cost log in `status.json`.
**Behaviour:** off unless `TVA_VLM_PROVIDER` set; input is redacted clip
or frames only; output notes may not state a price OCR did not read
(guard).
**Tests:** mocked transports; guard drops notes with numbers absent from
`ocr.parquet`.
**Exit:** one real chapter clip returns notes that cite frame ids; cost
per session logged.

### PR-16 — TradesViz fills (TVA4, later)

**Files:** `fills.py`, extra `tradevidanalyser[journal] = thesistester`
(git or PyPI once published), mirror loader `fills_mirror.py` when the
extra is missing, `tva fills <session> --executions <csv> --venue
topstepx|amp [--include-manual]`, `tests/fixtures/tradesviz_synthetic.csv`
+ `expected_fills.parquet` + `expected_trades.parquet` generated once by
the ThesisTester functions.
**Behaviour:** filter the CSV to fills whose UTC instant falls within the
session's `[start − 30 min, start + duration + 30 min]` window; write
`fills.parquet` / `trades.parquet` with `venue` and `tva_trade_id`; refuse
if zero fills (status `missing`, not error).
**Tests:** import path and mirror path produce identical frames; venue
column present; window filter; `commission/fees` discarded (assert not in
columns per TJ1 lock).
**Exit:** three mixed-venue real days parse and pair; counts match
TradesViz UI.

### PR-17 — AMP recon passthrough (TVA4, optional)

**Files:** `fills.py --reconcile-dir <thesistester journal output>` reads
`reconcile.json` and attaches `recon_status` per session date to
`trades.parquet` (additive column). No PDF parsing here.
**Exit:** `reconciled` shows on a day where the ThesisTester recon ran.

### PR-18 — Alignment (TVA5)

**Files:** `align.py` (prior = filename start; measurement = `ocr.parquet`
clock rows; robust linear fit (Theil–Sen) for offset/drift; confidence
from residual spread and sample count; fallback `method=filename`,
`confidence=0.6`), `tva align <session> [--manual-offset s]`.
**Tests:** synthetic samples with known offset/drift; outliers rejected;
low sample count → low confidence; manual override recorded.
**Exit:** on a real session, offset residuals ≤ 2 s at ≥ 90 % of samples;
`confidence ≥ 0.9`.

### PR-19 — Evidence (TVA5)

**Files:** `evidence.py` (map each `trades.parquet` row to video time via
alignment; select transcript segments in window; call extractor
`stated_fields` prompt (`prompts/stated_v1.de.md`) per window with
citations; attach nearest frames/ocr/clip), `tva evidence <session>`,
extend `pipeline._assert_citations` to evidence.
**Tests:** fake extractor; window boundaries; `alignment.confidence <
0.8` → `alignment: low` flag; trade without speech → `stated` all null +
gap.
**Exit:** golden excerpt: planted stop/target/playbook attach to the right
synthetic trade.

### PR-20 — Deterministic rules (TVA5)

**Files:** `rules.py`, `rules.yaml` (`daily_loss_limit_usd: null`,
`max_trades_per_day: 10`, `consecutive_loss_timeout: {n: 3, minutes: 30}`,
`post_loss_block_minutes: 5`, `reentry: {window_s: 120, max: 1,
block_minutes: 5}`, `flat_by: null`), `tva rules <session>`.
**Behaviour:** R-DLL, R-MAX10, R-3L30, R-5M, R-REENTRY, R-CLOSE from
`trades.parquet` only. R-DLL `unverifiable` while the limit is null (D4).
Fees: `net_pnl_currency` when present, else `gross_pnl_currency` with a
`fees_unknown` note.
**Tests:** table-driven synthetic trade sets per rule; boundary cases
(exactly 30 min; exactly 10 trades).
**Exit:** every deterministic rule returns pass/violated on a real
session with fills; D4 documented as the only `unverifiable`.

### PR-21 — Evidence-backed rules (TVA5)

**Files:** `rules.py` (R-PLAYBOOK, R-DEFINED, R-3C-CT, R-ARRIVAL,
R-SLTP*, R-HOURLY, R-ZONE, R-BIAS, R-TILT), `*R-SLTP` needs order
modifications which TradesViz lacks → always `unverifiable` with reason
until a venue adapter exists.
**Behaviour:** each rule reads `evidence.json` and `insights.session_events`;
absent speech → `unverifiable`, never `pass`.
**Tests:** synthetic evidence per rule; unverifiable semantics.
**Exit:** rule scorecard on a real session has no `pass` without a cited
segment.

### PR-22 — Context (TVA6)

**Files:** `context.py` (Notion read via `NOTION_API_KEY`: find the day's
`<D Mon YYYY> Macro Brief` / `NY session Brief` / `DRC ddmmyyyy` in the
Trading Journal DB; quote bias/conviction/kill levels verbatim; read
ThesisTester `attribution.parquet` / `zones.parquet` / `triggers.parquet`
from `--lab-dir` and join on `entry_fill_id`), `tva context <session>`.
**Tests:** Notion mocked; missing brief → `brief: null` + gap; lab join
by fill id; no recomputation.
**Exit:** `context.json` for a real day shows the brief's bias and the
lab's nearest level token per trade.

### PR-23 — Report (TVA6)

**Files:** `report.py` (fixed section order; facts rendered from files;
prose sections via `ReportProvider` (`fake`, `grok`) constrained to cite
ids), `prompts/debrief_v1.md`, `tva report <session>`.
**Tests:** every number in `debrief.md` appears in `trades.parquet`,
`ocr.parquet`, or `context.json` (regex digit audit); section order.
**Exit:** Accumu reads a real debrief in < 5 min and checks one finding
from its clip.

### PR-24 — Ledger + rollup (TVA6)

**Files:** `ledger.py` (DuckDB `ledger/ledger.duckdb`; tables `sessions,
trades, rule_checks, events`; `tva ledger add <session>`, `tva rollup
--week [--month]` → Markdown with adherence rates, violation counts,
trades/hour, stated-vs-lab agreement), API `GET /ledger/summary?weeks=4`.
**Tests:** idempotent add; rollup math on synthetic; API shape.
**Exit:** ten sessions in the ledger; weekly rollup renders.

### PR-25 — Notion publish (TVA6, optional)

**Files:** `publish.py` (`tva publish <session> --notion`: page titled
`<D Mon YYYY> Session Debrief`, tag *Trades Summary*, `Summaries` = one
sentence, `Learning 1–3` from candidates; update in place; one log line
on a *TVA runs* page). Off by default; the bot may keep doing this itself.
**Tests:** Notion mocked; idempotent update.
**Exit:** one real page created and updated without duplicates.

### PR-26 — Intent proposals (TVA7)

**Files:** `proposals.py` (map `stated.playbook`/`stated_levels` to the
ThesisTester `tag_map.yaml` vocabulary; emit `intent_proposals.json` +
`tradesviz_tags.csv`; `tva proposals <session>`; `tva proposals confirm
<id>` toggles status).
**Tests:** vocabulary mapping; CSV columns; status transitions.
**Exit:** one real week's proposals imported to TradesViz by hand and
visible as tags.

### PR-27 — Coach (TVA7)

**Files:** `coach.py` (`tva coach --weeks 4` reads ledger + last N
debriefs; provider `fake|grok`; output claims must reference ≥ N ledger
rows by id; exactly ≤ 1 experiment with `{rule_change, start, stop_criterion}`
appended to `ledger.experiments`), API `GET /coach/latest`.
**Tests:** claim citation count enforced; experiment schema.
**Exit:** first weekly review contains one ≥ 10-instance claim and one
running experiment.

---

## 6. Regression-safety envelope (every TVA PR)

| Rule | How TVA satisfies it |
|---|---|
| Additive | New stage = new file + new CLI verb + new API path; never changes an earlier file's schema without a version bump |
| Facts deterministic | Re-run tests for `session.json`, `fills`, `trades`, `alignment`, `rules` assert byte identity |
| Model outputs versioned | `provider/model/prompt_version` on every model file; prompts are files |
| Citations | `_assert_citations` extended per stage; tests include a fabricated-quote case |
| Numbers | Digit audit in report tests; spoken numbers stay `raw_text` |
| CI offline | Fake providers default; mocked transports; GPU/OCR extras optional and skipped |
| PII | Synthetic fixtures in git; `@golden` on NAS; redaction before frames leave |
| No broker writes | grep test for order endpoints; no venue API in this series |
| Two OSes | CI matrix `ubuntu` + `windows-latest` from PR-04 onward (ffmpeg via choco) |
| Same-PR docs | §7 table |

---

## 7. Docs each PR must touch

| PR | Docs |
|---|---|
| PR-02 | this file; `05_ROADMAP.md` status; `docs/README.md`; routine pack stub; `GOLDEN_EXCERPT.md` |
| PR-03 | `GLOSSARY.md` header; README `tva wer` |
| PR-04/05 | README install matrix; `03_STATE_OF_THE_ART.md` §1 decision check |
| PR-06 | `04_ARCHITECTURE.md` §2.1 → scripts |
| PR-08/09 | `prompts/README.md`; `04_ARCHITECTURE.md` §4 insights fields |
| PR-10/11 | README API; routine pack full; `04_ARCHITECTURE.md` §6 |
| PR-12–15 | `layout.yaml` comments; `02_SCOPE.md` §3.4; SOTA §2 VLM verification result |
| PR-16/17 | `04_ARCHITECTURE.md` §5; `02_SCOPE.md` §4.7 → in scope |
| PR-18–21 | `04_ARCHITECTURE.md` §4 alignment/rules; `05_ROADMAP.md` D4 decided |
| PR-22–25 | `04_ARCHITECTURE.md` §5/§6; routine pack "not yet" rows removed |
| PR-26/27 | routine pack coach section; `02_SCOPE.md` §4.12 → in scope |

---

## 8. Desk workflow (not repo tasks; the product is worthless without them)

1. OBS: Hybrid MP4; filename `%CCYY-%MM-%DD %hh-%mm-%ss`; mic on track 1;
   chapter hotkey bound; platform clock visible. Record to the PC SSD.
2. After the session: `tva watch` (PR-06) or a manual copy to
   `TVA_ROOT/recordings/`.
3. Record and redact one **golden excerpt** (`GOLDEN_EXCERPT.md`); hand-
   correct its transcript once. Put it at `TVA_ROOT/fixtures/golden/`.
4. Set `TVA_ROOT` on all three machines; run `tva doctor` on each.
5. Mac: `tva serve` under launchd; Tailscale (or tunnel) URL + token in the
   bot's secret store.
6. Weekly (TVA4+): export TradesViz executions CSV into
   `TVA_ROOT/journal/inbox/`; keep AMP PDFs there for ThesisTester recon.
7. Decide D4 (daily loss) when PR-20 lands; set it in `rules.yaml`.

---

## 9. Parked / follow-ups

- Full-session Gemini agentic pass as a weekly second opinion.
- TopstepX SignalR read-only listener stamping fills into OBS chapters
  during the session (zero alignment error; TopstepX-only; after TVA5).
- TopstepX API venue adapter for fills (only if TradesViz sync proves
  unreliable; never the spine).
- Read-only Streamlit/FastAPI HTML viewer (ThesisTester Studies pattern).
- Prosody features (rate, pauses) as `experimental` markers.
- `journal propose-study` (JS3) taking stated-setup frequency as input.
- Publishing `thesistester` to PyPI so the `[journal]` extra is a plain pin.

---

## 10. Agent prompt — PR-03 (next PR)

```text
You are implementing PR-03 from docs/IMPLEMENTATION_PLAN.md in the
TradeVidAnalyser repo. Read §0, §1, §2, §3.0, §3.2, §5 PR-03 and §6 in
full before writing code.

Hard rules:
- Additive only. Do not change session.json / transcript.json /
  insights.json field names. Do not touch serve.py routes.
- Fake providers stay the default. CI must not need a GPU, a model
  download, or an API key.
- New CLI verb `tva wer` returns JSON; exit 0 on success.
- Tests: glossary parse deterministic; WER known cases; jargon recall
  on a German sentence containing ONH / dVWAP / 3c; @golden test skips
  when TVA_ROOT/fixtures/golden is absent.
- Docs: GLOSSARY.md header; README `tva wer`; roadmap status row.
- Run `ruff check src tests` and `pytest -q`; both green before push.
```
