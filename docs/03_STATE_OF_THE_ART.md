# 03 — State of the art (Sep 2026) and what to use

Research notes behind the architecture choices. Prices and model names move
fast; each section ends with the decision and the reason, so a later swap is
a one-line change, not a redesign.

---

## 1. Speech-to-text (the backbone)

The commentary is the richest signal in the recordings, so ASR quality and
**word-level timestamps** matter more than anything else in the media stack.

| Option | Type | Strengths | Weaknesses | Indicative cost |
|---|---|---|---|---|
| **WhisperX** (faster-whisper large-v3 + wav2vec2 forced alignment) | self-hosted, GPU | Word timestamps accurate to ~20 ms (vanilla Whisper is off by 200–500 ms); 99 languages incl. German/English code-switching; ~70× realtime on an RTX 4090; `initial_prompt` for jargon | Setup (CUDA, HF token for pyannote if diarizing); batch only | GPU time only |
| NVIDIA Parakeet TDT 0.6B v3 | self-hosted | Fastest (RTFx ~3300), 25 European languages incl. German, word/segment/char timestamps, CC-BY-4.0 | Fewer languages than Whisper; less mature alignment ecosystem | GPU/CPU time |
| ElevenLabs Scribe v2 | hosted | Top-tier accuracy, 90+ languages, word timestamps, diarization | Audio leaves the machine | ~$0.22 / audio-hour batch |
| Deepgram Nova-3 | hosted | Cheap, fast, word timestamps, keyword boosting | Multilingual coverage narrower | ~$0.29 / audio-hour batch |
| AssemblyAI Universal-3.5 | hosted | Strong accuracy, rich add-ons | 18 languages on newest flagship | ~$0.15–0.40 / audio-hour |
| OpenAI `gpt-4o-transcribe` | hosted | Good accuracy, simple | Word timestamps weaker than aligned pipelines | ~$0.36 / audio-hour |

A 4-hour session ≈ 4 audio-hours ≈ $1–2 hosted, or free on a local GPU.

**Decision:** provider-pluggable ASR interface with **WhisperX as the default
local provider** on the trading PC GPU (jargon `initial_prompt`, **German
primary**, English level tokens), and **one hosted fallback** (ElevenLabs
Scribe v2 or Deepgram) for the Mac if it has no GPU. Skip diarization: mic
on its own OBS track; Grok is not on the tape today. Keep a 20-minute
hand-corrected German excerpt on the NAS (not in git) to measure WER
whenever the model changes.

**Decision check (PR-04):** Still the pick. `WhisperXAsrProvider` is wired
(`language=de`, glossary `initial_prompt` via `asr_options`, word alignment
on, silero VAD so no HuggingFace token, **no diarization**). `large-v3` is
the pinned model; `prompt_version` records `jargon-v1+<model>+whisperx-<ver>`.
Fake remains the CLI/CI default. Hosted fallback is unchanged and lands in
PR-05.

**Decision check (PR-05):** Hosted pick is **Deepgram Nova-3** (not Scribe v2)
because `keyterm` boosts the glossary list the Mac path needs. The adapter
uploads `audio/mic.opus` only; cost is estimated at the SOTA $0.29/audio-hour
and written to `status.json.cost_usd`. `ScribeAsrProvider` keeps the same
`AsrProvider` interface but is not wired. Fake remains the CLI/CI default.

---

## 2. Video understanding (multimodal LLMs)

Two very different tools exist now; use each for what it is good at.

### 2.1 Long-form: Gemini 3.x Flash agentic video

- Gemini 3.8 / 3.7 / 3.6 Flash accept video via File API or Cloud Storage; a 1M
  context handles up to ~3 h at low media resolution (~1 h at high).
- **Static mode:** 1 fps, ~70 tokens/frame at low res, audio at 1 kbps, a
  timestamp every second. A 4 h session ≈ 14,400 frames ≈ ~1M tokens ≈
  **≈$0.75 input at the introductory 3.8 Flash price** ($1.50 from Jan 2027),
  before output/thinking tokens.
- **Agentic mode** (`processing: "agentic"`): the model reads the transcript
  first, then zooms into time windows at adaptive fps (0.1–10 fps). Google
  reports up to 88 % fewer tokens and +7 % accuracy on long-form; Batch API
  halves the price again.
- Strength: "find every moment where X happens" over hours. Weakness: the
  whole video (with account numbers on screen) leaves the machine; 1 fps
  sampling misses fast DOM changes; the Enterprise platform caps a single
  video at ~45 min with audio, so files may need splitting.

### 2.2 Short-form: Grok 4.3 native video input

- Multiple developer guides (Apiyi, ChatForest, apidog, AI Learning Guides)
  report that **Grok 4.3 (API GA May 2026) accepts `video_url` content blocks**
  (mp4/mov/webm, ≤ 1080p, recommended ≤ 5 min, sampled at 1–4 fps, billed as
  image tokens; $1.25 / $2.50 per M in/out; 1M context). The official
  docs.x.ai page for video *understanding* was not reachable from this
  session, so **verify against docs.x.ai before depending on it** (TVA3 spike).
- Fit: a per-trade clip (entry −3 min … exit +2 min) is almost exactly the
  5-minute envelope. This is the natural way to let *Grok* look at the trade
  the way the user asked, without shipping the whole session.

### 2.3 Frame-level, deterministic

- Screen recordings have almost no scene cuts, so **PySceneDetect adds little**;
  use fixed stride plus change detection on regions of interest (positions
  panel, P&L, DOM).
- **PaddleOCR PP-OCRv5** (Apache-2.0; 5 M-param mobile model beats
  billion-parameter VLMs on OCR-only benchmarks) for reading numbers out of
  cropped panels: the platform clock (for alignment), position size, unrealised
  P&L, the instrument. Cheap enough to run on every second of a session on CPU
  if cropped.
- VLMs (Gemini / Grok / Claude / GPT) for *qualitative* frame questions ("is
  there an arrival candle close above the level at this point?") on a handful
  of keyframes per trade, never for reading numbers.

**Decision:** three layers, in order of trust: (1) OCR on ROIs for numbers and
the clock; (2) keyframes at fill timestamps for the record; (3) VLM on
per-trade clips (Grok 4.3 first, Gemini as the alternative) for qualitative
evidence, opt-in per provider. Full-session Gemini agentic pass is an
*optional* weekly deep-review, not the daily path.

**PR-12 (keyframes):** `tva frames` writes chapter ±0/2/5 s JPEGs under
`TVA_ROOT/sessions/<id>/frames/` from `layout.yaml` ROIs. No VLM provider is
wired and no frame leaves the store. **docs.x.ai `video_url` verification is
still pending (PR-15)** — the third-party Grok 4.3 reports in §2.2 are not a
confirmed API contract.

**PR-13 (ROI OCR):** `tva ocr` crops `clock` / `position` / `pnl` /
`instrument` and writes `ocr.parquet` (`t, roi, text, confidence, parsed`).
`OcrProvider` default is `fake` (`TVA_OCR_PROVIDER`). PaddleOCR is
`tradevidanalyser[ocr]` and is never required in CI. No VLM call is wired.

---

## 3. Commercial trade journals with AI (what exists, where the gap is)

| Product | AI today | Video / recording | Relevance |
|---|---|---|---|
| **TradeZella** ($29–49/mo) | *Zella AI* agents on every plan: Market Sentiment Briefing, **Auto-Tagger**, **Session Review** (compares the day with the morning plan, flags revenge trades and sizing deviations), Automated Backtesting; weekly/monthly reports | Tick-by-tick *market* replay; no screen-recording ingest | Closest analogue for a later *session review* page. TradeVidAnalyser v1 is the tape layer those products do not have. |
| TraderSync ($79.95 Elite) | Cypher AI pattern coaching, per-message limits | 250 ms market replay | Replay, not the trader's own recording |
| **TradesViz** (in use, $20–30) | Reactive AI Q&A over your data; 600+ stats | none | Stays the human journal + TJ source |
| Edgewonk ($197/yr) | Weekly *Edge Finder* email report; Tiltmeter psychology tracking | none | Confirms demand for psychology/adherence scoring |
| Tradervue | none | none | — |

**No product ingests the trader's own screen recording with live commentary.**
That is the v1 white space. Aligning the tape to fills and grading rule
adherence is a later slice of the same gap, and it is only reachable for
someone who already records every session (the DRC tech-check makes that a
habit here).

The desk also has something none of these products have: a personal
backtesting lab with a closed level vocabulary and a journal that attributes
fills to it. Joining commentary to *that* (did he say "ONH" when the lab says
the entry was at `ONH`?) is unique — and explicitly a later phase.

---

## 4. Agent platform: Grok Bot

- xAI's Grok Bot (beta 11 Aug 2026): persistent named bots, each account has a
  **cloud computer** with browser, filesystem, terminal; connectors/MCP;
  **skills** (recorded procedures); **routines** (cron or event triggers);
  agent-to-agent handoffs; human approval gates.
- The desk already runs ten bots this way (see `01_DISCOVERY.md` §2.2).
- xAI and third-party guides all recommend structured connectors/CLI over
  browser automation where possible ("websites change, block automation,
  present CAPTCHAs"). The Trade Importer's log confirms it.

**Decision:** Grok drives a **small local HTTP API** (`tva serve`) and never
scrapes. Processing is still a CLI the scheduler runs. Same "no embedded
agent" posture as `STUDY_RUNNER_GROK_ROUTINE_PACK.md`, different read
surface — because the bot cannot mount the NAS.

---

## 5. Fills (later): TradesViz, AMP, optional venue APIs

v1 has no fill ingest. When TVA4 opens:

- **TradesViz executions CSV** is the only source that already spans
  TopstepX and AMP. ThesisTester's `tradesviz_executions` profile is the
  proven parser (UTC, `spread_id`, tags/notes as intent, fees always 0).
- **AMP Daily Statement PDF** is FCM money-truth. Already parsed in
  ThesisTester `amp_statement` (confirmations, P&S, fee schedule, no
  timestamps). Call that; do not re-implement.
- **TopstepX / ProjectX Gateway API** exists (`Trade/search`,
  `History/retrieveBars`, $14.50/mo promo, key is full-trading scope, ToS
  allows read-only analytics on a private server). Useful as a *later
  adapter for one venue*. It cannot be the spine: it does not see AMP.

**Decision:** no fill client in v1. Next fill source is TradesViz, not a
broker API.

---

## 6. OBS and clock alignment

- Hybrid MP4 + `%CCYY-%MM-%DD %hh-%mm-%ss` filenames + creation time = wall
  clock start (Vienna). Chapter markers via hotkey / websocket let the trader
  drop a marker ("taking the trade", "check-in") that lands in the file
  metadata with millisecond precision.
- Residual clock error sources: OBS start latency (sub-second), PC clock drift
  vs exchange time (usually < 1 s with NTP), paused recordings, file splits.
- **Calibration:** OCR the platform clock (Quantower shows one) from a few
  frames spread across the session; fit offset (and drift if any); report
  confidence. Fall back to filename time with `alignment: medium`.

**Decision:** filename/creation time as the prior, OCR'd clock as the
measurement, ±2 s target, confidence stored in the Session Record.

---

## 7. Structured extraction with LLMs

- All major APIs (xAI, OpenAI, Google, Anthropic) support JSON-schema
  constrained outputs; use them so the Session Record's model-derived fields
  are typed and validated (Pydantic).
- Extraction runs **per window** (pre-entry / in-trade / post-exit; hourly
  slots) with the transcript segment ids in the prompt, and the schema forces a
  `segment_ids` citation on every extracted claim. Claims without citations are
  dropped at validation time.
- Keep prompts versioned files in the repo; store `model`, `prompt_version`,
  `schema_version` on every model-derived artifact so it can be regenerated.

**Decision:** xAI Grok as the default extraction provider (user preference,
cheap, structured outputs), behind the same provider interface as the ASR and
VLM layers so any can be swapped or run side by side for evaluation.

**Decision check (PR-08):** Confirmed. `GrokExtractProvider` is wired behind
`TVA_EXTRACT_PROVIDER=grok` with JSON-schema constrained `Insights` (from the
Pydantic model), German prompt `prompts/insights_v1.de.md`, windows of ≤ 40
segments (overlap 5), merge by `(seg, field)`, and citation-drop into
`gaps[]`. Default model is `grok-4.6` (`TVA_EXTRACT_MODEL`). Fake remains the
CLI/CI default; HTTP is mocked in tests.

---

## 8. Storage and analytics

- Facts: JSON (schema-versioned) + Parquet per session; a **DuckDB** file for
  ledger queries (columnar, single-file, zero-ops, reads Parquet directly).
- Media derivatives: Opus mono audio (~25 MB/h), JPEG keyframes, MP4 clips
  (stream-copied, no re-encode). Originals are copied to the NAS *after*
  OBS finishes (never recorded straight to SMB); the Session Record stores
  hashes and NAS-relative paths.
- Shared root: Synology on the LAN (`TVA_ROOT`). The Grok bot does not
  mount it; it calls `tva serve`.

---

## 9. What to watch (not now)

- Gemini agentic video on the *full* session as a weekly "second opinion"
  pass once clip-level review is stable.
- OBS ≥ 32 stream captions / live transcription → a real-time coach later.
- ProjectX SignalR `GatewayUserTrade` events → could stamp fills into OBS as
  chapter markers *during* the session (zero alignment error). Elegant, but
  TopstepX-only and it runs next to a live account; only as a read-only
  listener, only after fills exist, and never as the AMP path.
