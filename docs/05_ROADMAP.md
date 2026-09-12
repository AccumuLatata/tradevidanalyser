# 05 — Engineering roadmap

Numbered milestones, additive, each with an exit criterion a bot or a
human can check. No calendar estimates. A milestone is landed when the
exit holds on the golden excerpt **and** on one real session.

v1 is **TVA0–TVA3** (tape → API). Everything after that is a later
product decision, not a hidden dependency.

| Milestone | Intent | Depends on | Status |
|---|---|---|---|
| **TVA0** | Plan lock, decisions, desk prep, golden excerpt | — | PR-00/01/02 landed; see [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) |
| **TVA1** | Ingest + German transcript | TVA0 | PR-03…PR-07 landed; fake ASR default |
| **TVA2** | Insights extract + headless API (`tva serve`) | TVA1 | PR-08 Grok extractor landed; PR-09 session events + summaries; fake extract default |
| **TVA3** | Optional frames / clock OCR / chapter clips / opt-in VLM | TVA2 | |
| **TVA4** | TradesViz executions join (broker-agnostic fills) | TVA2 | later |
| **TVA5** | Alignment + per-trade windows + rule scorecard | TVA4 | later |
| **TVA6** | Briefs + ThesisTester attribution + debrief/ledger | TVA5 | later |
| **TVA7** | Coach loop: intent-tag proposals, experiments | TVA6 | later |
| parked | Full-session Gemini pass; live fill → OBS chapters; viewer; TopstepX API as a *venue adapter* | — | parked |

---

## TVA0 — Plan lock and desk preparation

Nothing to code except, when we start TVA1, a repo skeleton.

**Decisions:** D1–D9 are recorded in the table below. Open follow-ups are
only "NAS is on the desk yet?" and "how the Mac API is reached from Grok"
(Tailscale vs tunnel) — both are wiring, not product forks.

**Desk settings (one-time, can start now):**
- OBS: Hybrid MP4; filename `%CCYY-%MM-%DD %hh-%mm-%ss`; mic on its own
  audio track; "Add chapter marker" hotkey. Platform clock visible on
  screen (helps TVA3 / later alignment). **Record to the trading PC disk,
  not to the NAS.**
- After the session: a copy (or robocopy / rsync) of the MP4 onto
  `TVA_ROOT/recordings/`.
- Hand-correct one **golden excerpt** (20–30 min of a real session,
  redacted): German commentary, a spoken bias, a named playbook, one
  stated stop/target, one hourly check-in. This is the WER reference.
  Keep it on the NAS, not in git.

**Exit:** this file's decision table is the desk's position; golden
excerpt exists on the future `TVA_ROOT`.

## TVA1 — Ingest and transcript

- `tva ingest`: ffprobe, wall-clock start from filename + creation-time
  cross-check, chapters, mic-track Opus, hashes, `session.json`. Split
  OBS files stitch to one session.
- `tva transcribe`: provider interface + WhisperX (local GPU) + one
  hosted adapter; German primary; jargon `initial_prompt` from
  `docs/GLOSSARY.md`; word timestamps; per-segment language.
- WER harness against the golden excerpt.
- `tva doctor` for ffmpeg / GPU / `TVA_ROOT`.

**Exit:** golden + one real session produce `transcript.json`; jargon
recall ≥ 90 %; `session.json` re-runs byte-identical.

## TVA2 — Insights and API

- `tva extract`: constrained JSON, German input, citations required,
  `fake` provider in CI. Fields as in `04_ARCHITECTURE.md` §4. English
  briefs stay out of this stage — Grok already has them and can compare.
- `tva serve`: `GET /health`, `/sessions`, `/sessions/latest`,
  `/sessions/{id}`, `/transcript`, `/insights`. Read-only except
  `POST /sessions/{id}/run` bound to localhost / Tailscale.
- Example JSON committed for contract tests (no media).
- Draft `docs/TVA_GROK_ROUTINE_PACK.md`: URL, schema, hard rules.

**Exit:** a `curl` from another machine on the LAN (and, once tunneled,
from a throwaway request) returns last session's insights; every quote
round-trips to a real segment.

## TVA3 — Frames (feasibility slice)

- Keyframes at chapter markers.
- Optional ROI OCR of the platform clock (calibration contact sheet).
- Optional short clips around chapters (ffmpeg stream copy, mic only).
- Optional VLM notes on those clips, redaction mask on balance/account
  ROIs, cost-logged, off by default.

**Exit:** a chapter clip opens at the right moment; OCR clock, if
enabled, is within 1 s of filename time on the golden excerpt.

---

## Later phases (not scheduled)

**TVA4 — TradesViz fills.** Loader for the executions export (reuse or
contract-test ThesisTester's `tradesviz_executions` profile). Venue is a
column. AMP PDF is optional money-truth via ThesisTester `amp_statement`.
No TopstepX API required. Exit: three mixed-venue days parse.

**TVA5 — Align + rules.** Video clock ↔ fill clock; per-trade windows;
deterministic + evidence-backed catalog. **D4 is decided here**, not
before.

**TVA6 — Context join.** Notion briefs (English) + DRC + ThesisTester
`journal attribute|zones|triggers`. Optional `tva report` / publish.
This is the first time the two repos must be installed on the same
machine (likely the Mac, where the study store already lives).

**TVA7 — Coach loop.** Proposed TradesViz tags from speech; weekly
experiment tracking.

---

## Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| German ASR drops English level tokens | insights miss the actual setup name | jargon glossary + WER on those tokens |
| LLM invents a quote | destroys trust | schema-enforced citations; drop uncited claims |
| OBS written straight to SMB | dropped frames, corrupt Hybrid MP4 | record local, copy after (desk rule) |
| NAS copy still running when the bot calls | empty `/sessions/latest` | `status: ingesting`; watchdog; PC should transcribe before copy *or* copy-then-process with a lock file |
| Mac disk / GPU too small to process | pipeline stuck if PC is off | hosted ASR fallback; prefer PC GPU |
| Grok cannot reach the API | bots unused | Tailscale or tunnel; `/health` on Sunday audit |
| Scope creeps into ThesisTester / TopstepX | delayed v1 | this file's v1 cut; no TT import in package metadata |
| Videos or frames leave the LAN | PII | API default = JSON only; media endpoints opt-in + redaction |

---

## Decisions (locked 12 Sep 2026)

| # | Decision | Locked position | Notes |
|---|---|---|---|
| **D1** | Processing / serving hosts | **All three machines must work** (trading PC, laptop, Mac) via `TVA_ROOT`. **Default process: trading PC** after the session (fastest). **Default serve: Mac** (always on, Study B box). | Laptop is the same CLI. Bot cloud computer is HTTP-only, never a processor. |
| **D2** | Fill source | **Not TopstepX-exclusive. Not v1.** When fills land: **TradesViz executions** (venue-agnostic). AMP Daily Statement PDF for money-truth if needed (ThesisTester already parses it). TopstepX API is an optional later *adapter* for one venue, never the spine. | Matches how ThesisTester TJ is built. |
| **D3** | Artifact / video sync | **Synology NAS on the LAN** as `TVA_ROOT`. Record local, copy after. Until the NAS exists, a folder on the Mac. **Grok reaches JSON via `tva serve` on the Mac + Tailscale or Cloudflare tunnel** — not via a NAS mount. | This was the "Google Drive vs S3" question. The NAS replaces that for LAN machines; the tunnel is the remaining hop. |
| **D4** | Daily loss $100 vs $200 | **Parked.** Irrelevant until TVA5 rule checks. The video API does not grade P&L. | See §"Why D4 existed" below. |
| **D5** | Language | **Videos / ASR: German.** Briefs stay English (already produced). Insights store **German quotes verbatim**; Grok may write English notes from them. Jargon list is bilingual (German speech, English level tokens). | |
| **D6** | Providers | ASR: **WhisperX local on the PC GPU**, hosted fallback (Scribe v2 or Deepgram) from the Mac if needed. Extraction: **xAI Grok**, constrained JSON. VLM: opt-in later, Grok 4.3 or Gemini after a docs check. | Adapters, so any row can swap. |
| **D7** | Grok voice on the tape | **No.** Mic-only track is enough. Desktop audio optional, not required. | Separate tracks remain an OBS recommendation in case that changes. |
| **D8** | Practice vs live tape | **Real trading videos.** Golden excerpt is a redacted real session, not a practice account. | Still no order-sending code. |
| **D9** | ThesisTester store / data gravity | **Mostly the Mac** (Study B, always on). Disk is tight → **NAS becomes the bulk store** (recordings, Session Records, eventually large study artifacts if you choose). Journal CLI stays where the store is; TVA v1 does not need it. | Good idea — see architecture §2.1. |

### Why D3 was confusing

The question was not "which cloud bucket." It was: **the Grok bot does
not sit on your LAN.** Something has to move *small* artifacts (or an
HTTP response) from the machine that has the video to the machine that
has the bot. Drive/S3/git-LFS were candidates when the shared disk was
undefined. A Synology share solves PC ↔ Mac ↔ laptop. It does **not**
by itself solve Grok. `tva serve` + Tailscale is that hop.

### Why D4 is not a video-analyser question

R-DLL ("did he breach the daily loss limit?") is a **fills** rule. The
plan says $200, the DRC says $100. That conflict only matters when we
score a day against realized P&L. v1 does not have P&L. When TVA5
exists, the limit is a config value on the rule engine, not a
hard-coded constant — decide it then, in the same place the rest of the
rulebook is reconciled.

---

## Non-goals (so the bots do not drift)

Do not build a UI. Do not compute levels. Do not import ThesisTester in
v1. Do not make TopstepX the spine. Do not let the coach edit facts.
Do not add order endpoints. Do not record OBS onto the NAS. Do not
upload raw video by default.
