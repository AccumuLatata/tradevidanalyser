# 02 — Scope and focus

Working name: **Debrief** (series code **DF**). Rename freely; the code is only
used to number milestones the way ThesisTester does (TJ, JS, RS…).

---

## 1. One-sentence job

> Turn each recorded trading session into a time-stamped, verifiable **Session
> Record** (what was said, seen and done, aligned to the actual fills), and
> from it produce a **daily debrief** and a **longitudinal coaching ledger**
> that the Grok bots can act on — without a human having to open a video.

The desk's stated job is *"help Accumu make more money as a discretionary day
trader of NQ/ES."* ThesisTester attacks that from the *location* side (does
this level have edge?). Debrief attacks it from the *behaviour* side (did the
trader do what the plan says, and what does it cost when he doesn't?). Both feed
the Edge Finder.

---

## 2. Who uses it

| User | Mode | What they need |
|---|---|---|
| **Debrief bot** (new Grok bot, evening routine) | primary, headless | A CLI that ingests the newest session, produces artifacts, and returns a machine-readable status. Then it writes the Notion page and pings only if something needs attention. |
| **Edge Finder bot** (exists) | reader | Per-trade evidence + ThesisTester attribution, to judge live behaviour against lab results. |
| **Coach conversation** (any bot, on demand) | reader | Query the ledger: "show me every re-entry violation in the last 20 sessions with the clip". |
| **Accumu** (human) | occasional | Read the Notion debrief; jump to a clip when he disagrees with a finding; confirm/reject proposed tags. Never required to run anything. |
| **ThesisTester journal** (`journal …` CLI) | downstream | Executions CSV + proposed intent tags, exactly in the format TJ already loads. |

Design consequence: **every output is a file with a stable schema**; every
finding carries **provenance** (timestamps, transcript segment ids, frame ids)
so a bot or a human can check it in seconds.

---

## 3. In scope (v1 = DF0–DF6)

1. **Session ingest** — detect finished OBS recordings, probe them, extract
   audio tracks, register the session (wall-clock start, duration, chapters).
2. **Transcript** — word-timestamped ASR with a trading-jargon vocabulary,
   German/English code-switching, stored per session.
3. **Fills** — pull fills for the session from the TopstepX API (read-only);
   pair into round trips using ThesisTester's journal contract; emit the CSV
   TradesViz / TJ already accept.
4. **Alignment** — one clock. Map video time ↔ wall clock ↔ fill time, with a
   calibration step (filename start time, OBS creation time, optional OCR of
   the on-screen platform clock) and a reported confidence.
5. **Trade evidence** — for each round trip: transcript window, stated
   setup/bias/stop/target if spoken, keyframes at entry/exit, OCR of the
   positions/P&L region, a short clip file.
6. **Session events** — hourly check-ins, bias statements, trade / no-trade
   zone calls, tilt language, references to the brief or to Grok, rule
   mentions.
7. **Rule checks** — deterministic checks from the rule catalog (§6) with
   `pass / violated / unverifiable` and evidence refs. Never inferred from an
   LLM alone when fills can decide it.
8. **Daily debrief** — Markdown + JSON: source strip, day summary, per-trade
   table, rule scorecard, contrast with the day's Macro/NY brief, three
   candidate learnings, gaps. Written to the Notion Trading Journal by the bot.
9. **Ledger** — append-only per-session metrics (SQLite/DuckDB + Parquet) for
   weekly / monthly rollups and trend questions.
10. **Bot routine pack** — copy-ready prompts and hard rules for the Debrief
    bot, mirroring `STUDY_RUNNER_GROK_ROUTINE_PACK.md`.

## 4. Out of scope (v1)

- Any order-transmitting code path. The TopstepX client will not implement
  order/position endpoints at all (import-time guard), so a misconfigured bot
  cannot trade.
- A GUI. A read-only viewer is a *later* option (ThesisTester's Studies
  viewer pattern), not a v1 deliverable.
- Rebuilding what exists: TradesViz statistics, ThesisTester level
  attribution / triggers / counterfactuals, brief generation.
- Live, in-session coaching (real-time). Everything here is post-session.
- Real-time video analysis or full-frame "watch the whole chart" understanding.
  Visual analysis is scoped to regions of interest and per-trade clips.
- Emotion recognition from face/webcam. Only speech content and, optionally,
  simple prosody features (rate, pauses) labelled *experimental*.
- Multi-user / multi-account.

---

## 5. Principles (inherit the desk's culture)

1. **Facts, then interpretation, never blended.** The Session Record is facts
   with provenance. The debrief's "Interpretation" and "Learnings" sections
   are clearly labelled as model output and cite record ids.
2. **Never invent numbers.** Prices, P&L, times, counts come from fills and
   OCR with confidence, or are marked *missing*. A transcript quote is a
   quote, not a price.
3. **Fail closed on alignment.** If the video↔fill clock offset cannot be
   established within tolerance (target ±2 s), trade evidence windows are
   marked `alignment: low` and rule checks that depend on timing become
   `unverifiable`.
4. **Deterministic first.** Anything a rule engine can decide from fills
   (trade count, consecutive losses, re-entry timing, daily loss) is decided
   deterministically; the LLM only reads speech and frames.
5. **Idempotent, resumable, content-addressed.** Re-running on the same video
   and fills produces byte-identical facts. Model outputs carry model +
   prompt version so they can be regenerated.
6. **Quiet on success.** Bot pings only for violations, gaps or failures.
7. **PII stays local by default.** Videos show account numbers and balances.
   Hosted models receive audio and cropped/redacted frames only, and only
   when the operator has opted in per provider.
8. **CLI is the contract.** No embedded agent, no MCP server, no queue. Same
   posture as ThesisTester's routine pack.

---

## 6. Rule catalog (what the coach can grade)

Decidable from fills alone (deterministic):

| id | Rule | Check |
|---|---|---|
| R-DLL | Daily loss limit ($200 plan / $100 DRC — **one value must be chosen**) | realized P&L incl. fees never below limit; flag first breach time |
| R-MAX10 | ≤ 10 trades per day | round-trip count |
| R-3L30 | 3 consecutive losses → 30 min timeout | gap between 3rd loss exit and next entry ≥ 30 min |
| R-5M | Loss → 5 min block before a new trade | gap ≥ 5 min after any losing exit |
| R-REENTRY | Stopped on entry → max one re-entry, then 5 min block | detect entry/stop pairs within N seconds at same level/direction |
| R-CLOSE | Flat by session end (if the plan says so) | last exit before cutoff |

Decidable from fills + transcript (evidence-backed):

| id | Rule | Check |
|---|---|---|
| R-PLAYBOOK | Trade follows a named playbook, no on-the-fly entries | a playbook name (or its alias) is spoken in the window before entry |
| R-DEFINED | Entry, invalidation, target stated before entry | stop/target words + numbers in pre-entry window |
| R-3C-CT | Counter-trend needs 3c confirmation | stated "counter" / "CTR" + "3c" mention; cross-check with ThesisTester `journal triggers` |
| R-ARRIVAL | Wait for arrival candle close | spoken cue and/or entry timestamp vs 1m bar close |
| R-SLTP | SL/TP only moved on new information | fill/order modifications (API) vs stated reason |
| R-HOURLY | Hourly check-in (xx:50) | check-in language near each xx:50 ± 5 min |
| R-ZONE | Trades only in declared trade zones | "no-trade zone" declared → no entries until revoked |
| R-BIAS | Bias re-evaluated every 5–15 min / agile | frequency of bias statements; contradiction with brief noted |
| R-TILT | TILT noticed → cool-down | tilt language followed by ≥ N min without entries |

Everything not covered above is *observation*, not a rule check (e.g. "you
mentioned the HVL 14 times and traded through it twice").

---

## 7. Success criteria for v1

- A finished session produces a Session Record and a debrief **without human
  input**, on the evening of the session, in under 30 minutes of compute on
  the chosen host.
- Alignment confidence ≥ 0.9 on ≥ 90 % of sessions (measured against the
  OCR'd platform clock).
- Every rule check in §6 marked deterministic returns pass/violated on 100 %
  of sessions with fills; evidence-backed checks return `unverifiable` rather
  than guessing when speech is absent.
- Transcript WER on trading jargon ≤ 10 % on a hand-checked 20-minute sample
  (measured once in DF1, tracked when the model changes).
- The Trade Importer routine can be switched off because Debrief delivers the
  TradesViz-importable CSV (DF2 exit criterion).
- Accumu reads the debrief and, in at least three of the first ten sessions,
  finds one finding he did not know about *and can verify from the clip*.

---

## 8. Headless: the recommendation

Yes — build it headless, bot-first. Reasons specific to this desk:

- The bots already own every other artifact (briefs, imports, lab logs) and
  the operating conventions (quiet on success, one log line, source strip,
  Sunday audit) are established. A UI would be a second, unmaintained surface.
- ThesisTester proved the pattern: CLI + artifacts + a routine pack. The
  bots use it reliably; the parts of the desk that use browsers (Trade
  Importer) are the parts that break.
- Grok Bot's cloud computer has a terminal and filesystem; a CLI is the most
  reliable surface it has.

Three nuances so "headless" does not become "opaque":

1. **The bot is the user, but the human is the judge.** Every finding needs a
   2-second way to be checked: a timestamped quote, a keyframe, or a 30–90 s
   clip. Artifacts are designed for that, not for a dashboard.
2. **Headless ≠ cloud.** The videos are born on the trading PC and are large
   (see `04_ARCHITECTURE.md` §2). The heavy media step should run where the
   video is; only small artifacts travel to where the bot is. "Headless" here
   means *no UI*, not *runs on the bot's machine*.
3. **Two model roles, kept apart.** The app itself uses a model for
   *extraction* (structured, cited, low temperature). The Grok bot uses a
   model for *coaching* (conversational, cross-session, opinionated). The
   first is inside the CLI and versioned; the second is a prompt in the
   routine pack. Do not let the coach edit the facts.
