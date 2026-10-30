# Phase 2 — Sidecar facts (PR-37)

**Status:** Plan (PR-37, docs-only) plus owner facts recorded in PR-38 (D7 / D8 / Q-15 = B). PR-39 ships the opt-in `<stem>.jsonl` copy. **PR-40** ships `sidecar.py` facts and `time_anchor.json` (Q-15 = B, measured per clip; 1.043 s loud fallback). **PR-42** ships `TVA_NET_FEES` (Vierer-Set only, TT:892 / 903): facts mirror (lots, positions, costs, `account`), net only under the flag, publish only with a `locked` weekly manifest, desk rules from `accounts.yaml`, R-MAX10 positions-or-lots.
**Stand geprüft:** TVA `main` @ `674a9bf` (PR-37 landed; PR-36 @ `c6029b8`). ThesisTester source: extract of `docs/research/TVA_TT_Research_Plan.md` with original line numbers (Revision 4, stand 12.10.2026; TT lesestand `251b5099`, TVA `c6029b8`) (TT:6).
**Danach:** Review. Sidecar implementation remains the later series below. PR-38 does not start that code.
**Authoritative source:** AccumuLatata/ThesisTester `docs/research/TVA_TT_Research_Plan.md` (German), via the attached line-numbered extract. `docs/JOURNAL_EXTENSION_PLAN.md` is **not** in that extract and stays [NV].
**Markers** (TT:28–39): **[C]** code or docs self-read; **[T]** self-run on `/tmp` copies; **[Q]** vendor primary; **[Q2]** weaker secondary; **[R]** own arithmetic; **[G]** set in the briefing, not self-checked; **[Rv]** finding from the review of PR #631 on Accumu’s raw data (adopted, not self-checked here); **[NV]** not verified, deliberately not guessed. N4 path checks below are **[C]** at `4cd647f5dc56405ca4705c60fb59c61dc499aea9` (base `4191dd5`); TT:900 left-column cites remain `c6029b8`. TT **[Rv]** is only used where the extract itself uses it.

**Not in this file:** Phase 1 multi-clip / pause behaviour ([`multi-clip-day.md`](multi-clip-day.md)). Roadmap D1–D9 ([`../05_ROADMAP.md`](../05_ROADMAP.md)). Research-plan D7 / D8 / Q-15 are not those roadmap ids. Off-limits (TT:19): `~/thesistester` @`0ebc1494`, `~/tt_mw/rg_pilots`, `~/tt_mw/rg_reruns`, and the Run-2 outputs.

---

## 1. Scope

PR-37 is the Phase 2 plan, docs only (TT:898). It records:

- sidecar contract with pause rule and `file` check (§7.1)
- tolerances, first-frame offset ≈ 1.043 s as expected value, band 0.8–1.3 s
- flag table
- contract v2 with **coach independence** (R8; already recorded here, implemented later in PR-46) (TT:885, 907)
- coach profile without judgments, account field, CD list, B.5 rules
- three locked statements: **N4**, **N5**, **Q-15**

It does not implement flags, parsers, tests, or goldens. Code is forbidden in this PR (TT:898).

Companion already landed: Phase 1 [`multi-clip-day.md`](multi-clip-day.md). Phase 1 left Phase 2 as the coach data contract plus a second, independent compute from the raw export (`multi-clip-day.md:779`) [C].

---

## 2. Series order

Source: TT §10.3 (TT:883–907), order R6 / R10 (TT:885).

| Order | PR | Notes |
|---|---|---|
| 1 | **PR-37** | This file. Docs only (TT:898). |
| 2 | **PR-38** | Autostart guard (TT:899). D7 checked 2026-10-29: nothing registered, nothing to disable. |
| 3 | **PR-43** | No silent fallbacks. Default, no flag (N4). After PR-38 (TT:885, 900). |
| 4 | **PR-39** | Copy sidecar (this implementation). D8 resolved (TT:901, 923). |
| 5 | **PR-40** | Sidecar reader and time anchor. After Q-15 (TT:441, 902, 1100). |
| 6 | **PR-42** | Facts mirror. After JX1, JX2, JX8 (TT:885, 903). |

**Deferred:** PR-41, PR-44 (only if Deepgram is used), PR-45, PR-46 (TT:885, 904–907). Coach independence (R8) is already in PR-37; PR-46 later puts it on the v2 contract (TT:885).

These are series numbers, not GitHub pull-request numbers. They continue Phase 1 (last landed series PR-36 @ `c6029b8`) [C].

---

## 3. Sidecar contract

Source: TT §7.1 RQ-1 (TT:435–468) and PR-40 row (TT:902).

### 3.1 Pause rule (R13)

Source: TT:447–458, 902.

Media time continues at the pause `video_s`. The `resume` value is a plausibility check only (band **0.4–0.8 s**). Each section is anchored on its `resume` wall clock (TT:449, 902).

A section runs from `record_start` or a `resume` to the next `pause` or `record_stop` (TT:450). Measured resume-minus-pause is about 0.58–0.6 s and does not grow (TT:448) [Rv] in the extract.

### 3.2 File check and fail modes (R14)

Source: TT:443–446, 459–462, 467, 902.

- Sidecar path is `<stem>.jsonl` beside `<stem>.mkv` / `<stem>.mp4`, not `.events.jsonl` (TT:460, 901).
- TVA rejects a sidecar whose `file` does not match the video: **name equal**, and **duration ±0.1 s vs `ffprobe`**. Otherwise status `sidecar_mismatch`, no time anchor (TT:462, 467, 902).
- No `record_stop` (crash) → `incomplete` (TT:444).
- No `record_start` (script loaded late) → `no_sidecar` (TT:445).
- Clock / NTP on ALTrading is [NV] in the source itself (TT:446).

### 3.3 First-frame offset and other tolerances

Source: TT:439–441, 463–468, 902. First-frame measurement [Rv] in the extract (#13).

| Check | Rule |
|---|---|
| First-frame offset | Expected value ≈ **1.043 s** after `record_start`. Band **0.8–1.3 s** per OBS version. Outside → sidecar `invalid`, reason `first_frame_offset_out_of_band` (TT:440, 465, 902). |
| Span residual | `\|wall_span − video_span − first_frame_offset\| ≤ 0.10 s` (TT:464). |
| Resume band | 0.4–0.8 s; outside → `resume_offset_out_of_band` on that section (TT:449–452, 466, 902). |
| Sidecar duration | ±0.1 s vs video / `ffprobe` (TT:467, 902). |
| Fill ↔ spoken time | ≤ 2 s (TT:468). |

`time_anchor.json` stamps `first_frame_offset_source: fixed | measured`. A fixed value must not silently replace a missing measurement (TT:441). **Q-15 = B** (Accumu, 2026-10-29): measure the first-frame offset per clip as `wall_span − video_span` (TT:439–440): `record_stop`−`record_start` wall minus pause spans, minus ffprobe duration. `start_time` below 0.1 s is unusable. 1.043 s is a loud fallback (warning on every clip plus `offset_source: fallback`, never silent). Implemented in PR-40. Extract text at TT:441 / TT:1100 still called this open; the owner decision is recorded here.

### 3.4 Per-section judgment (N5) and alignment method (M2)

Source: TT:450–457, 898(b), 902.

- Sections **before** a resume outlier stay `verified`.
- From the outlier on (that section and every later one): `segment_invalid` with reason `resume_offset_out_of_band`. Media time is no longer attested.
- Fills in a `segment_invalid` section get **`video_s = null`**, reason `segment_invalid`. **No** silent fallback onto a filename estimate or the pause detector. **No** legacy path for those sections (TT:453, 902).
- The sidecar as a whole is `partial` with the section list; `time_anchor.json` holds per-section status (TT:454).
- **M2 alignment method** (TT:455, 902):
  - `verified` → method `sidecar`
  - `partial` → `sidecar` for `verified` sections, `null` for `segment_invalid` sections
  - unchanged legacy path **only** when there is **no** valid sidecar: `no_sidecar` / `invalid` / `sidecar_mismatch`, and then always with that status
  - a `partial` sidecar never falls back to legacy as a whole
- JX8 reports the day `video_incomplete`, with the affected fills (TT:456, 898).
- With **`TVA_PAUSE_GUARD`**: verified sidecar pauses **replace** the detector (`pause_checks.method = sidecar`) (TT:902).

Revision 3 made the whole sidecar `invalid` on one outlier; this rule replaces that (TT:457).

---

## 4. Flag table

Source: TT:887–894, 605. Phase 1 allowed sets stay in `multi-clip-day.md` §3.6 [C]. Unset/empty is off. Truth is `1` / `true` / `yes` / `on` (`flags.py:16`, `multi-clip-day.md:537`) [C].

| Flag | PR | Allowed combination |
|---|---|---|
| `TVA_SIDECAR` | 39, 40 | Orthogonal: combinable with every **existing allowed** row (TT:891). |
| `TVA_NET_FEES` | 42 | Only together with the Vierer-Set `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES`, `TVA_DAY_PUBLISH`, `TVA_TRADING_HOURS`, because it changes day numbers (TT:892). |

**Other combinations → `ungültige Flag-Kombination`** (TT:894). That string is already the Phase 1 abort (`flags.py:17` `INVALID_FLAG_SET_ERROR`) [C]. Every new row ships with a test in `flags.py` (TT:894, 605).

**`TVA_STRICT_FACTS` is dropped.** Revision 2 had it. It is gone: PR-43 is default without a flag (R6) (TT:894, 606, 900).

`TVA_SIDECAR` does not, by itself, license `TVA_NET_FEES` without the four-flag set.

`TVA_ALLOW_SESSION_ID_OVERWRITE` remains orthogonal (Phase 1 §3.6) [C]. This plan does not change that.

N4 (PR-43) is the confirmed exception to the Phase 1 “new behaviour behind a default-off flag” rule (TT:606, 898).

---

## 5. Contract v2 — coach independence (R8)

Source: TT:885, 907. §7.2 heading is in the extract (TT:470); the §7.2 body is **not** in the extract [NV].

R8 is recorded in this PR-37 plan. PR-46 later implements the v2 HTTP contract (TT:885).

Contract text (TT:907): the coach recomputes positions and net independently from the **raw TopstepX export** and reports differences as a **finding**. v1 stays unchanged. A deviation must not be resolved silently. `/v2/…` endpoints carry facts only; each record has `account`. An unknown field is an error (PR-46 tests; not this PR).

Phase 1 placeholder is the same idea (`multi-clip-day.md:779`) [C]. No extra schema fields are invented here.

---

## 6. Coach profile, CD list, B.5

### 6.1 Profile

Source: TT:26, 612, 898, 907.

- **No judgments** in new fact files. TVA delivers facts, not judgments; no silent fallbacks (TT:26, 612).
- **Account field** on each v2 record (`account`) (TT:898, 907).
- **CD list** is required on the profile (TT:898). This extract **does not enumerate** the CDs [NV]. Phase 1 CD1–CD14 stay in `multi-clip-day.md` §6 and are not copied here [C].
- **B.5** ops rules (below).

### 6.2 B.5 ops rules

Source: TT:21–26, 595–598, 613, 899, 911. Quoted, not extended.

- **ALTrading:** no agent and no automatic run. Nothing outside `C:\whisperx`, no drivers, no autostart, GPU only after trading. Any change to scheduled tasks (`schtasks`), including D7, Accumu does himself, by hand, after trading (TT:23, 595, 613).
- **NAS:** agents and box only via SFTP, read-only, only `/trading`. Accumu places files only under `/trading`. Workers never read the NAS (no `/mnt/nas-trading`, no SMB) (TT:24, 595, 598).
- **Tick runs:** `--workers` ≤ 9. Committed YAML stays `workers: 1` (TT:25, 596).
- No PR ships scripts or tasks that run by themselves on ALTrading (TT:595). `tva doctor` on ALTrading only after trading (TT:613, 899).
- TVA delivers facts, not judgments; no silent fallbacks (TT:26).

Tick-weekend / farm-slot rules in TT:597 are ThesisTester ops, not a TVA code change in this PR.

---

## 7. Mandatory statements

Wording is locked (TT:898).

### N4 — PR-43 is default behaviour, no flag

> N4: PR-43 becomes default behaviour without a flag (exception confirmed by Accumu 2026-10-12); list affected entry points; `fake` only when set explicitly; no real ASR provider = fail loudly.

Source: TT:606, 898(a), 900. Confirmed exception to the Phase 1 flag rule (TT:606). A silent fallback is a briefing violation, not protected legacy (TT:606).

**PR-43 target (TT:900):** unset `get_asr_provider` / `get_ocr_provider` / extract provider → error “Provider fehlt”; `fake` only when set explicitly. Without a real ASR provider every run aborts loudly: exit ≠ 0, message **`TVA_ASR_PROVIDER fehlt`**. `tva serve` `POST /sessions/{id}/run` answers with an error plus message, **never** placeholder text.

**Affected entry points — source list (TT:900), desk-checked @ `4191dd5` then implemented in this PR [C].** Unset factories raise `TVA_*_PROVIDER fehlt`. `fake` is explicit opt-in and warns.

| Source cite (TT:900) | After PR-43 [C] | Desk-check |
|---|---|---|
| `providers/asr.py:367-370` `get_asr_provider` | `asr.py:368-371`: `require_provider_name` → unset raises `TVA_ASR_PROVIDER fehlt` | **Implemented.** `fake` / `test` only when set; warning `TVA_ASR_PROVIDER=fake (explizit gesetzt)`. |
| `ocr.py:26, 133` `get_ocr_provider` | `ocr.py:130-133`: same helper, env `TVA_OCR_PROVIDER` | **Implemented.** No `DEFAULT_PROVIDER`. |
| `providers/extract.py:882` extract provider | `extract.py:882-890`: same helper, `keyword` is also explicit fake | **Implemented.** |
| `pipeline.py:64` ASR | `pipeline.py:64` `get_asr_provider(provider_name)` | **Match.** |
| `pipeline.py:83` extract | `pipeline.py:83` `get_extract_provider(provider_name)` | **Match.** |
| `pipeline.py:196` OCR | `pipeline.py:196` `get_ocr_provider(provider_name)` | **Match.** |
| `evidence.py:321` extract | `evidence.py:325` `get_extract_provider(provider_name)` | **Match.** |
| `pause_guard.py:401, 419, 559` OCR | `pause_guard.py:401`, `:419`, `:559` each call `get_ocr_provider()` | **Match.** Today `:401-403` already aborts fake OCR with `Guard braucht echten OCR-Provider` (`pause_guard.py:35`) — **different message** from `TVA_ASR_PROVIDER fehlt`; ASR is not involved here. |
| CLI `tva transcribe` | `cli.py:397-399` → `transcribe_session` | **Match.** |
| CLI `tva extract` | parser `cli.py:74-76`; handler `:401-403` → `extract_session` | **Match.** |
| CLI `tva run` | parser `cli.py:81-88`; handler `:410-415` → `run_latest` → transcribe + extract (`pipeline.py:522-523`) | **Match.** |
| CLI `tva ocr` | parser `cli.py:118-124`; handler `:458-462` → `ocr_session`. Help no longer says “fake (default)” | **Match.** |
| CLI `tva evidence` | parser `cli.py:181-190`; handler `:498-505` → `evidence_session` (extract at `evidence.py:325`) | **Match.** |
| CLI commands that start OCR via `pause_guard` | `tva align` → `run_pause_guard` (`align.py:271-272`); `tva evidence` / `tva rules` → `ensure_pause_check` (`evidence.py:448-451`, `rules.py:1393-1396`, call at `:1396`); `tva day audit --sample-clocks` is read-only samples (`cli.py:254-258`, dispatch `:529`) | Source does not name each command. These are the `c6029b8` callers that reach `pause_guard` OCR. |
| `tva watch --run` `watch.py:402-404` | `watch.py:402-404`: if `run`: `transcribe_session` then `extract_session` | **Match.** |
| `tva serve` `POST /sessions/{id}/run` `serve.py:206-282` | Route `serve.py:208-242`; `_require_run_providers` `:215-218` / `:266-270` raises HTTP **400** with `TVA_*_PROVIDER fehlt` before the worker starts. Worker still at `:288+` (`transcribe` then `extract`). No placeholder body | **Implemented.** Unset provider is a failed HTTP body with the message, not 202 + later `status.error`. Configured (including explicit `fake`) still 202. |
| `scripts/windows/copy-after-obs.ps1` line 21 | Line 21: `tva --root $Root watch --source $Source --once` — **no** `--run` | **Match.** Does not reach ASR today. Stays runnable. Anyone who adds `--run` there needs a set provider (TT:900). |

**Not on the source N4 list (TT:900).** Previous draft listed `tva doctor` and a bare `TVA_ASR_PROVIDER` env row as run entry points. They are **not** in the PR-43 row. Doctor remains a Phase 1 / PR-38 surface (TT:899): it does not run ASR. Unset ASR / OCR / extract is **warn** (`TVA_*_PROVIDER fehlt`) so `ok` stays true on an install that never runs OCR; `/health` uses the same `run_doctor` report.

**Listed expectation changes (TT:900 / 606):**

- `tests/test_asr.py::test_default_asr_provider_is_fake` → unset raises `TVA_ASR_PROVIDER fehlt`.
- `tests/conftest.py` sets `TVA_ASR_PROVIDER`, `TVA_OCR_PROVIDER`, `TVA_EXTRACT_PROVIDER` to `fake` (autouse).
- Doctor / `/health`: unset ASR / OCR / extract is `warn` (`TVA_*_PROVIDER fehlt`); new `ocr_provider` check. `report.ok` stays true when only those are unset.
- `test_l0_with_guard_on_matches_snapshot`: L0 `evidence.json` files are **not** rewritten. Regenerated clipped windows keep the same `t0`/`t1` and add `window_clipped` + raw bounds; the test strips those new fields before compare.

PR-43 is this implementation. N4 records the locked default and the list.

### N5 — sidecar resume outlier is per-section

> N5: sidecars judged per section; sections before a resume outlier stay `verified`, from the outlier on `segment_invalid` with no video time for their fills; JX8 reports the day `video_incomplete`.

Source: TT:450–456, 898(b), 902. Accumu decided 12.10.2026 (TT:450). Full rule in §3.4.

### Q-15 — first-frame offset rule is B (measured per clip)

> Q-15 = B: measure the first-frame offset per clip; 1.043 s is a loud fallback (warning plus a flag in the output, never silent).

Source text (TT:441): whether the first-frame offset is used as a **fixed** value (1.043 s) or **estimated per clip** (e.g. from `ffprobe` start time and `record_start`). Accumu decides before PR-40; PR-37 holds the decision. Repeated at TT:1100.

**Owner fact (Accumu, 2026-10-29), recorded in PR-38:** variant **B** — per clip as `wall_span − video_span` (TT:439–440). The 1.043 s figure remains the expected value / loud fallback (TT:440, 465), never a silent substitute for a missing measurement (TT:441). `time_anchor.json` stamps `first_frame_offset_source: fixed | measured` and, on fallback, `offset_source: fallback`. Implemented in PR-40.

---

## 8. Open decisions

These are research-plan desk tasks / questions, not roadmap D1–D9.

Owner facts below are Accumu 2026-10-29. They are not in the extract (TT:922–923, 441, 1100 still describe the tasks / question). Not [C] from the extract.

| Id | Gate | Extract text | Owner fact (2026-10-29) |
|---|---|---|---|
| **D7** | Before **PR-38** (TT:899) | Check whether `tva-watch` is registered on ALTrading; if yes, disable it by hand. Accumu only, after trading (TT:922). PR-38 documents “nicht für ALTrading (B.5)” and a read-only `doctor` query; Accumu disables by hand (TT:899). | Checked on the trading PC: no scheduled task matches tva/tradevid; no TVA entries in the Startup folders or HKCU/HKLM Run keys. Nothing has to be disabled. |
| **D8** | Before **PR-39** (TT:923) | Clarify whether `T:\tradevid` points at the NAS (SMB). If yes, that conflicts with “NAS only SFTP, only `/trading`” and is resolved **before PR-39** (TT:923, 24). | There is no T: drive. The TVA folder is `Z:\tradevid`; Z: is the SMB share `\\AHORNBUSINESS\trading`. **Resolved in PR-39:** scripts/docs no longer say `T:\tradevid`. Sidecar dest is `$TVA_ROOT/recordings/<stem>.jsonl` from `--root` / `TVA_ROOT`, never a hardcoded NAS / UNC path. |
| **Q-15** | Before **PR-40** | Extract: fixed first-frame offset (≈ 1.043 s) vs estimate per clip (TT:441, 1100). | **B:** measured per clip; 1.043 s is a loud fallback (warning plus a flag in the output, never silent). Recorded here; implementation is PR-40. |

D1–D6 and D9–D11 are Accumu desk tasks in TT:913–926. They are not gates on this PR series beyond what the rows above say. Not restated as new product decisions.

---

## 9. Source line index

ThesisTester `docs/research/TVA_TT_Research_Plan.md` (extract):

| Lines | Topic |
|---|---|
| 6 | Lesestand TT `251b5099`, TVA `c6029b8` |
| 19 | Off-limits Run-2 trees (not opened) |
| 21–26 | B.5 ops; facts not judgments |
| 28–39 | Marker table |
| 435–468 | §7.1 sidecar contract (pause R13, file R14, N5, M2, tolerances, Q-15) |
| 441, 1100 | Q-15 |
| 470 | §7.2 heading only; body not in extract [NV] |
| 595–613 | B.5 again; TVA PR-37 ff. rules; N4 exception |
| 883–907 | §10.3 series, flags, PR rows |
| 885 | Order; R8 already in PR-37 |
| 889–894 | Flag table; `TVA_STRICT_FACTS` dropped; `ungültige Flag-Kombination` |
| 898 | PR-37 row (this file) |
| 900 | PR-43 / N4 entry points |
| 902 | PR-40 / file check / N5 / M2 / pause_checks.method |
| 907 | Vertrag v2 / R8 text |
| 922 | D7 |
| 923 | D8 |

`docs/JOURNAL_EXTENSION_PLAN.md` is **not in the extract** [NV]. Not restated.

This repo [C] @ `c6029b8`:

| Path | Topic |
|---|---|
| `docs/plans/multi-clip-day.md:537-560` | Phase 1 allowed flag rows; Vierer-Set |
| `docs/plans/multi-clip-day.md:779` | Phase 2 placeholder |
| `src/tradevidanalyser/flags.py:16` | `ungültige Flag-Kombination` already |
| N4 table in §7 | Provider factories, pipeline, evidence, pause_guard, CLI, watch, serve, `copy-after-obs.ps1:21` |

---

## 10. PR-39 shipped (copy only)

Source: TT:901. `TVA_SIDECAR` is orthogonal (TT:891). Unset/empty is off; behaviour then matches main (video only, no sidecar key).

| Surface | Flag on | Flag off |
|---|---|---|
| `watch` / `ingest` | copy exactly `<stem>.jsonl` next to the recording (`watch.py` `VIDEO_SUFFIXES`, `sidecar_copy.py`). Already-ingested watch attaches a missing/late sidecar the same way ingest does, only if the dest sidecar is absent, and only after the video lock (skip the write if the lock is held; classify-only stays unlocked when dest is present) | no `.jsonl` copy |
| Pattern | `<stem>.jsonl` only; `.events.jsonl` is not copied | n/a |
| Events | `sidecar: ok\|missing\|invalid_json` plus watch-report counts | no `sidecar` field |
| Dest | `TVA_ROOT/recordings/<stem>.jsonl` from `--root` / `TVA_ROOT` | n/a |

**Not in PR-39 (now PR-40):** `sidecar.py` facts, pause/file/first-frame checks, `time_anchor.json`, alignment method.

**D8:** remaining `T:\tradevid` examples in `tva-watch.xml`, `copy-after-obs.ps1`, README, and `04_ARCHITECTURE.md` §2.1 are `Z:\tradevid` or `TVA_ROOT`. No UNC sidecar dest in code.

## 11. PR-40 shipped (reader + time anchor)

Source: TT:902, Q-15 = B (TT:441, 1100; owner 2026-10-29). `TVA_SIDECAR` remains orthogonal (TT:891). Unset/empty is off; then no `time_anchor.json` and alignment is unchanged (L0-identical).

| Surface | Flag on | Flag off |
|---|---|---|
| `sidecar.py` | parse `<stem>.jsonl` including `file_changed`; pause rule R13; N5 per-section; file + duration ±0.1 s; first-frame band 0.8–1.3 s | not called |
| Q-15 | measure `wall_span − video_span` per clip (TT:439–440): record_stop−record_start wall minus pause spans, minus ffprobe duration. `start_time` &lt; 0.1 s unused. Failed measure → **1.043 s** + warning on every clip + `first_frame_offset_source: fixed` + `offset_source: fallback` | n/a |
| `time_anchor.json` | `schema_version "1"` under `sessions/<id>/`; M2 method `sidecar` / `null` | not written |
| `TVA_PAUSE_GUARD` | verified/partial sidecar replaces the detector (`pause_checks.method = sidecar`) | detector unchanged |

Owner [Rv] goldens (Test 1 / Test 2 / 17:11:06 / T-S2 / T-S3) are **not** committed. Tests use synthetic fixtures; [Rv] cases are skipped until the owner confirms values.

## 12. Out of scope for PR-40

PR-40 implements the TT:902 reader. This file stays the Phase 2 plan.

- No `TVA_NET_FEES` (PR-42).
- No golden regeneration. Unclipped / no-media-error fixtures stay bit-identical.
- No merge.
- No Program B results.
- Report / Notion / coach / VLM factories keep their existing defaults (not on the N4 list).
- Doctor is not an N4 run entry point (TT:900); unset ASR / OCR / extract is warn (`TVA_*_PROVIDER fehlt`), not fail.

## 13. PR-42 shipped (`TVA_NET_FEES` + facts mirror)

Source: TT §10.3 (TT:883–907). Flag row TT:892 / 894. Series order after JX1, JX2, JX8 (TT:885, 903). Unset/empty is off; then pairing, L0, and unlocked clip publish stay as on main.

| Surface | Flag on | Flag off |
|---|---|---|
| `TVA_NET_FEES` | Only with the Vierer-Set `TVA_EXCLUSIVE_FILLS` + `TVA_DAY_RULES` + `TVA_DAY_PUBLISH` + `TVA_TRADING_HOURS` (those four already require pause + manifest). `TVA_SIDECAR` does not license it (TT:892, 894) | ignored; combo check skipped |
| Other sets | `ungültige Flag-Kombination` (TT:894) | n/a |
| `fills_mirror.py` | Flag-off only (TJ1). Flag on reads a TT journal dir; a TradesViz CSV is `Netto ohne Kostenquelle` | TJ1: costs discarded, net equals gross |
| `tva publish` | TT `week_manifest.json` written by `lock_files` (tick `prepare_manifest` required) and verified by `validate_week_manifest` + `assert_manifest_inputs_match` + `assert_claimed_lock_supported` (report.py:654-657). Inputs are `inputs: [{role, path, sha256}]`. Hand-edited `locked` (`lock_status_inconsistent`) / tampered hash (`input hash mismatch`) cannot unlock. `DayStale` carries the date | clip publish / existing lock rules unchanged |
| Desk rules | TT `accounts.yaml` (per-instrument `fee_schedule`, date `trading_days`) plus TVA `desk_rules.yaml` account → desk. Missing account → `Konto fehlt`. Topstep desk never applied to other accounts | `rules.yaml` as on main |
| R-MAX10 | counts JX2 `position_id`s from `--positions-dir`; if none, counts lots and stamps `count_basis: lots` plus `count_basis_reason` | existing evidence (`count` / `max` / `trade_ids` only) |

Independent recomputation stays with the coach (R8 / PR-46). `docs/JOURNAL_EXTENSION_PLAN.md` is still not in the extract [NV].
