# 02 — Scope and focus

Working name: **TradeVidAnalyser** (series code **TVA**). Earlier drafts used
*Debrief* / DF; the job is the same, the v1 cut is smaller.

---

## 1. One-sentence job

> Turn each recorded trading session into a time-stamped, verifiable **Session
> Record** (what was said, and what can reasonably be read from the screen)
> and expose it through a **small headless API** so Grok can coach from the
> tape — without a human opening a video.

The desk's stated job is *"help Accumu make more money as a discretionary day
trader of NQ/ES."* ThesisTester attacks that from the *location* side (does
this level have edge?). TradeVidAnalyser attacks it from the *behaviour*
side (what did the trader say and do on the tape?). Those two products meet
later, on purpose — not in v1.

AMP is the destination broker; TopstepX is a current venue. The journal that
already spans both is **TradesViz**. This app must not grow a TopstepX-only
spine.

---

## 2. Who uses it (v1)

| User | Mode | What they need |
|---|---|---|
| **Grok bots** (coach / research / any) | primary | `GET /sessions/latest` (and by date) returning structured insights + transcript excerpts with timestamps. Quiet on success; they already know how to write Notion. |
| **Accumu** | occasional | Read the insights Grok cites; jump to a timestamp or a clip when he disagrees. Never required to run anything. |
| **Scheduler on the trading PC** | writer | After OBS stops: copy the recording to the NAS (and, if the PC is doing the heavy work, run ingest + transcribe). |

**Not a v1 user:** ThesisTester's `journal …` CLI. It becomes a consumer in
TVA4+ when we choose to join fills.

Design consequence: **every output is a file with a stable schema** on a
shared root (NAS). The API only serves those files. Every finding carries
**provenance** (timestamps, transcript segment ids, optional frame ids).

---

## 3. In scope — v1 (TVA0–TVA3)

The original ask, and the right first cut: a small headless service that
feeds Grok from the videos.

1. **Session ingest** — find finished OBS recordings on any configured
   machine, probe them, extract the mic track, register the session
   (wall-clock start, duration, chapters, hashes).
2. **Transcript** — word-timestamped ASR, **German primary**, with a trading
   jargon vocabulary (English level tokens inside German speech: ONH, dVWAP,
   3c, …). Stored per session.
3. **Session insights** — structured, cited extraction from the transcript:
   spoken bias, playbook names, stated stop/target, hourly check-ins, tilt
   language, brief references, hesitation, rule mentions. Quotes are quotes;
   no invented numbers.
4. **Optional visual notes** — keyframes at chapter markers; ROI OCR of the
   on-screen clock (helps later alignment); short clips around chapters.
   Full-frame "watch the chart" is out. A VLM pass on clips is opt-in.
5. **Headless API** — local HTTP on the always-on Mac, reading the NAS root.
   List sessions, return transcript + insights + status. No UI.
6. **Bot routine pack** — copy-ready prompt: where the API lives, what the
   JSON means, hard rules (never invent a quote; cite segment ids).

## 4. Later (explicitly not v1)

These are real, and they are how the tape becomes expensive in a good way.
They wait until v1 is boring and reliable.

7. **Fills from TradesViz** (TVA4) — executions CSV, broker-agnostic
   (TopstepX today, AMP as the goal, anything TradesViz already journals).
   AMP Daily Statement PDF is money-truth if we need fees/P&S; ThesisTester
   already parses it (`amp_statement`). Not a TopstepX API client.
8. **Clock alignment + per-trade evidence windows** — video time ↔ fill
   time, once fills exist.
9. **Rule scorecard** — the catalog in §6, including the $100 vs $200 daily
   loss question. Needs fills. Parked (see D4).
10. **ThesisTester join** — `journal attribute` / `zones` / `triggers` next
    to spoken setup names. "He said ONH; the lab tagged pdPOC."
11. **Daily debrief page + coaching ledger + Notion publish** — Grok can
    already write Notion; v1 just has to give it facts. A dedicated publish
    command is optional later.
12. **Intent-tag proposals back into TradesViz.**

## 5. Out of scope (all phases unless reopened)

- Any order-transmitting code path, any broker.
- A GUI.
- Rebuilding TradesViz statistics or ThesisTester level math.
- Live, in-session coaching.
- Emotion recognition from face/webcam.
- A TopstepX-only (or AMP-only) architecture.
- Multi-user.

---

## 6. Principles

1. **Facts, then interpretation, never blended.** The Session Record is
   facts with provenance. Insights labelled *interpretation* cite record ids.
2. **Never invent numbers.** A transcript quote is a quote, not a price.
   Prices and P&L appear only when a later fills/OCR stage supplies them.
3. **Broker-agnostic journal.** When fills arrive, they arrive as TradesViz
   executions (and optionally AMP statements). Venue is a field, not a
   product fork.
4. **ThesisTester is a later consumer, not a dependency.** v1 must run on a
   machine that has never installed ThesisTester.
5. **Idempotent, resumable, content-addressed.** Re-running on the same
   video produces byte-identical facts. Model outputs carry model + prompt
   version.
6. **Quiet on success.** Bots ping only for gaps or failures.
7. **PII stays on the LAN by default.** Videos show account numbers and
   balances. Hosted models receive audio and cropped/redacted frames only,
   and only when opted in per provider. The NAS is private; the API is not
   on the public internet without a tunnel the operator chose.
8. **Files are the source of truth; the API is a window.** CLI and API call
   the same functions. No embedded agent, no MCP server, no queue.

---

## 7. Rule catalog (parked until TVA4+)

Kept so it is not forgotten. **None of this is a v1 deliverable.** D4
(daily loss $100 vs $200) only matters here.

Decidable from fills alone (deterministic):

| id | Rule | Check |
|---|---|---|
| R-DLL | Daily loss limit (value is a config, not a constant) | realized P&L incl. fees never below limit |
| R-MAX10 | ≤ 10 trades per day | round-trip count |
| R-3L30 | 3 consecutive losses → 30 min timeout | gap after 3rd loss |
| R-5M | Loss → 5 min block | gap after any losing exit |
| R-REENTRY | Stopped on entry → max one re-entry, then 5 min | same level/direction |
| R-CLOSE | Flat by session end (if the plan says so) | last exit before cutoff |

Decidable from fills + transcript (evidence-backed):

| id | Rule | Check |
|---|---|---|
| R-PLAYBOOK | Named playbook, no on-the-fly entries | playbook spoken before entry |
| R-DEFINED | Entry, invalidation, target stated before entry | stop/target in pre-entry window |
| R-3C-CT | Counter-trend needs 3c | speech + later `journal triggers` |
| R-ARRIVAL | Wait for arrival candle close | cue and/or entry vs 1m close |
| R-SLTP | SL/TP only moved on new information | modifications vs stated reason |
| R-HOURLY | Hourly check-in (xx:50) | check-in language near xx:50 |
| R-ZONE | Trades only in declared trade zones | "no-trade zone" → no entries |
| R-BIAS | Bias re-evaluated / agile | frequency; contradiction with brief |
| R-TILT | TILT noticed → cool-down | tilt language then quiet |

v1 may *extract* the spoken events (check-in, tilt, playbook name) without
grading them against fills.

---

## 8. Success criteria for v1

- After a session, artifacts land on the NAS and `GET /sessions/latest`
  returns insights **without human input**.
- Transcript is usable German: WER on trading jargon ≤ 10 % on a
  hand-checked 20-minute sample (English tokens inside German speech count
  as jargon).
- Every insight field that is a quote cites a transcript segment that
  actually contains that text.
- A Grok bot can, from the API alone, produce a useful session note that
  Accumu can check against the tape in under five minutes.
- The app runs against the NAS root on the trading PC, the Mac, and a
  laptop with only a path/env change.

---

## 9. Headless API: the recommendation

Yes — headless, bot-first, **API as the bot surface**, CLI as the operator
and scheduler surface. Reasons specific to this desk:

- The original idea was a small API that feeds Grok. That is still the
  right v1. ThesisTester-style CLI is how *processing* is invoked; it is
  not how a cloud bot should discover last night's session.
- Grok's cloud computer cannot mount a Synology share. It can `curl` an
  API on the always-on Mac (Tailscale / Cloudflare tunnel).
- A UI would be a second, unmaintained surface.

Three nuances:

1. **The bot is the user, the human is the judge.** Every finding needs a
   2-second check: a timestamped quote, a keyframe, or a short clip.
2. **Headless ≠ cloud.** Heavy media runs on the LAN (trading PC GPU
   preferred). The API is a window onto LAN files.
3. **Two model roles, kept apart.** The app extracts (structured, cited,
   low temperature). Grok coaches (conversational, opinionated). The coach
   does not edit facts.
