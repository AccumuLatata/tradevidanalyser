# Phase 2 — Sidecar facts (PR-37)

**Status:** Plan. No code, configuration, test, fixture, or golden change in this PR.
**Stand geprüft:** `main` @ `c6029b8` (PR-36 landed).
**Danach:** Review. Implementation is the later series below, each PR reviewed on its own. This PR does not start that code.
**This PR:** docs-only, no named parity test.
**Authoritative source:** AccumuLatata/ThesisTester `docs/research/TVA_TT_Research_Plan.md` (German) and `docs/JOURNAL_EXTENSION_PLAN.md`. Line cites below use those files.
**Access:** This agent's GitHub App token can read only `AccumuLatata/tradevidanalyser`. Clone and contents API for `AccumuLatata/ThesisTester` return 404. Facts that exist only in ThesisTester are marked **[NV]** and restated only from the PR-37 task extract. Nothing else is added. Program B Run 2 results, `results/` trees, and ThesisTester `docs/ROLL_PRIOR_LEVEL_GATE_IMPLEMENTATION_PLAN.md` line 25 were not used.
**Markers:** **[Rv]** = checked against this repo at `c6029b8`. **[NV]** = not opened in ThesisTester in this environment. The `[Rv]` / `[NV]` convention is TT plan lines 38–39 [NV].
**Not in this file:** Phase 1 multi-clip / pause behaviour. That stays in [`multi-clip-day.md`](multi-clip-day.md). Roadmap D1–D9 stay in [`../05_ROADMAP.md`](../05_ROADMAP.md). Research-plan D7 / D8 / Q-15 below are not those roadmap ids.

---

## 1. Scope

PR-37 records the Phase 2 sidecar contract, flag rows, coach contract v2, and the three locked statements N4, N5, and Q-15. It does not implement flags, parsers, tests, or goldens.

Companion (this repo, already landed): Phase 1 plan [`multi-clip-day.md`](multi-clip-day.md). Phase 1 said Phase 2 is the data contract for the Trading Coach (transcript, manifest, clock only when verified, completeness counts, no judgments) plus a second, independent computation from the raw export (`multi-clip-day.md:779`) [Rv].

---

## 2. Series order

Source: TT `TVA_TT_Research_Plan.md` §10.3 lines 883–907; PR-37 row line 898 [NV].

| Order | PR | Notes |
|---|---|---|
| 1 | **PR-37** | This file. Docs only. |
| 2 | **PR-38** | After D7. |
| 3 | **PR-43** | After PR-38. Default behaviour; see N4. |
| 4 | **PR-39** | After D8. |
| 5 | **PR-40** | After Q-15. |
| 6 | **PR-42** | After PR-40. |

**Deferred:** PR-41, PR-44, PR-45, PR-46 [NV].

These are series numbers, not GitHub pull-request numbers. They continue `docs/IMPLEMENTATION_PLAN.md` / Phase 1 (last landed series PR-36 @ `c6029b8`) [Rv].

---

## 3. Sidecar contract

Source: TT §7.1 around line 440; Q-15 at lines 441 and 1100 [NV]. Also `JOURNAL_EXTENSION_PLAN.md` lines 830–845 and 1139–1147 [NV] (wording not opened here).

### 3.1 Pause rule

Media time continues at the pause `video_s`. The resume value is a plausibility check only, not a clock reset. Plausibility band: **0.4–0.8 s** [NV].

### 3.2 File check

A file check is part of the sidecar contract (TT §7.1 ~440) [NV]. Fail modes and required sidecar path are not restated here; they were not opened in ThesisTester.

### 3.3 Tolerances — first-frame offset

Expected first-frame offset is about **1.043 s**. Acceptance band: **0.8–1.3 s** [NV].

Whether that 1.043 s is a fixed constant or is measured per clip is **Q-15**, still OPEN (section 7).

---

## 4. Flag table

Source: TT §10.3 flag lines 889–894; PR-37 row line 898 [NV].

Phase 1 allowed production sets stay as written in `multi-clip-day.md` §3.6 [Rv]. Ungeset or empty is off. Truth is `1`, `true`, `yes`, or `on` (`multi-clip-day.md:538`) [Rv].

| Flag | Combines with | Notes |
|---|---|---|
| `TVA_SIDECAR` | Freely with every **allowed** Phase 1 row | New in this series. Not implemented in this PR. |
| `TVA_NET_FEES` | **Only** together with `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES`, `TVA_DAY_PUBLISH`, `TVA_TRADING_HOURS` | Same four flags as the Phase 1 Vierer-Set (`multi-clip-day.md:543-546`) [Rv]. Not implemented in this PR. |

`TVA_SIDECAR` does not, by itself, license `TVA_NET_FEES` without that four-flag set.

`TVA_ALLOW_SESSION_ID_OVERWRITE` remains orthogonal (Phase 1 §3.6) [Rv]. This plan does not change that.

No other new flags are named here.

---

## 5. Contract v2 — coach independence (R8)

Source: TT coach / TVA rules lines 602–612 [NV].

Contract **v2**: the coach is independent of TVA’s derived positions and net (R8). The coach recomputes positions and net from the **raw TopstepX export**. Differences against TVA (or other derived numbers) are reported as **findings**, not silently adopted [NV].

This matches the Phase 1 placeholder: “Fakten-Spezifikation und die zweite, unabhängige Rechnung aus dem Rohexport” (`multi-clip-day.md:779`) [Rv]. Schema fields for that finding object are not invented here.

---

## 6. Coach profile

Source: TT coach / TVA rules lines 602–612; B.5 / ops lines 23–24 [NV].

The Phase 2 coach profile:

- carries **no judgments**
- includes an **account** field
- includes the **CD list**
- follows **B.5** ops rules: **no autostart / no agent on ALTrading**; **NAS only via SFTP read-only `/trading`**

The CD list itself is not copied from Phase 1 CD1–CD14. Those remain in `multi-clip-day.md` §6 [Rv]. The ThesisTester list at lines 602–612 was not opened [NV].

Full ops-rule wording at TT lines 23–24 was not opened [NV]. Only the B.5 constraints above are recorded.

---

## 7. Mandatory statements

These three statements are locked for this series. Wording is fixed.

### N4 — PR-43 is default behaviour, no flag

> N4: PR-43 becomes default behaviour without a flag (exception confirmed by Accumu 2026-10-12); list affected entry points; `fake` only when set explicitly; no real ASR provider = fail loudly.

Source: TT §10.3 / exception note (task extract; exact TT line for the Accumu 2026-10-12 exception not separately numbered in the extract) [NV].

Today every ASR path defaults to `fake` when `TVA_ASR_PROVIDER` and `--provider` are unset (`src/tradevidanalyser/providers/asr.py:1`, `asr.py:367-368`) [Rv]. PR-43 removes that default. `fake` is used only when it is set explicitly. If no real ASR provider is configured, the process fails loudly.

**Affected entry points (this repo @ `c6029b8`) [Rv]:**

| Surface | Path | Today |
|---|---|---|
| Provider factory | `src/tradevidanalyser/providers/asr.py` `get_asr_provider` | `name or TVA_ASR_PROVIDER or "fake"` |
| Session transcribe | `src/tradevidanalyser/pipeline.py` `transcribe_session` | calls `get_asr_provider(provider_name)` |
| CLI | `tva transcribe --provider` (`cli.py`) | `--provider` default `None` → factory fake |
| CLI run | `tva run` → `run_latest` (`pipeline.py:512-522`) | `transcribe_session` with no provider name |
| Watch | `tva watch --run` (`watch.py`) | `transcribe_session` with no provider name |
| HTTP | `POST /sessions/{id}/run` transcribe stage (`serve.py:280-282`) | `transcribe_session` with no provider name |
| Doctor | `tva doctor` (`doctor.py:97`) | treats unset `TVA_ASR_PROVIDER` as `"fake"` |
| Env | `TVA_ASR_PROVIDER` | unset → fake |

A source-plan list that differs from this inventory was not opened [NV]. This table is the current code, not a new API.

PR-43 is not this PR. N4 only records the locked default.

### N5 — sidecar resume outlier is per-section

> N5: sidecars judged per section; sections before a resume outlier stay `verified`, from the outlier on `segment_invalid` with no video time for their fills; JX8 reports the day `video_incomplete`.

Source: TT §10.3 / N5 (task extract) [NV].

No extra statuses are invented. The three tokens in the statement are the ones that stand: `verified`, `segment_invalid`, `video_incomplete`.

### Q-15 — first-frame offset rule is OPEN

> Q-15 explicitly OPEN (fixed 1.043 s vs measured per clip), must be decided before PR-40.

Source: TT lines 441 and 1100 [NV].

Do not treat 1.043 s as locked for PR-40 work until Accumu decides.

---

## 8. Open decisions

These are research-plan decisions, not roadmap D1–D9.

| Id | Gate | What is recorded here |
|---|---|---|
| **D7** | Before **PR-38** | Open. Question text: TT line 922 [NV]. Not roadmap D7 (Grok voice on the tape). |
| **D8** | Before **PR-39** | Open. Question text: TT line 923 [NV]. Not roadmap D8 (practice vs live tape). |
| **Q-15** | Before **PR-40** | OPEN: fixed 1.043 s vs measured per clip (section 7). |

Source: TT lines 922–923 (D7/D8) and 441 / 1100 (Q-15) [NV].

---

## 9. Source line index

ThesisTester `docs/research/TVA_TT_Research_Plan.md` [NV] (file not opened in this environment):

| Lines | Topic |
|---|---|
| 23–24 | Ops rules (B.5 excerpt recorded in §6) |
| 38–39 | `[Rv]` / `[NV]` markers |
| ~440 | §7.1 sidecar contract |
| 441 | Q-15 |
| 602–612 | Coach / TVA rules; contract v2 R8; coach profile |
| 883–907 | §10.3 series (PR-37 at 898; flags 889–894) |
| 922 | D7 |
| 923 | D8 |
| 1100 | Q-15 |

ThesisTester `docs/JOURNAL_EXTENSION_PLAN.md` [NV] (file not opened):

| Lines | Topic |
|---|---|
| 830–845 | Journal-extension companion (not restated) |
| 1139–1147 | Journal-extension companion (not restated) |

This repo [Rv]:

| Path | Topic |
|---|---|
| `docs/plans/multi-clip-day.md:538-560` | Phase 1 allowed flag rows; Vierer-Set |
| `docs/plans/multi-clip-day.md:779` | Phase 2 placeholder (coach contract + independent raw-export compute) |
| `src/tradevidanalyser/providers/asr.py:367-368` | ASR default `fake` (N4 inventory) |
| `src/tradevidanalyser/pipeline.py:54-64`, `:512-522` | transcribe / `tva run` |
| `src/tradevidanalyser/cli.py` `transcribe` | CLI provider default |
| `src/tradevidanalyser/watch.py` `--run` | watch transcribe |
| `src/tradevidanalyser/serve.py:280-282` | HTTP transcribe |
| `src/tradevidanalyser/doctor.py:97` | doctor treats unset ASR as fake |

---

## 10. Out of scope for this PR

- No code, tests, fixtures, or goldens.
- No named parity test.
- No merge, no ready-for-review flip on the implementation series.
- No Program B results.
- No new sidecar JSON schema beyond the pause rule, bands, and N5 tokens above.
- No implementation of `TVA_SIDECAR`, `TVA_NET_FEES`, or PR-43.
