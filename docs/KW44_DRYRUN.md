# KW44 dry-run (2026-10-26 to 2026-10-30)

Short summary of the box reports (REPORT, COACH_REVIEW, ROOTCAUSE) plus
the rerun after GitHub #46–#49. No account IDs, UUIDs, API keys, local
home paths, or real export filenames.

Times are Europe/Vienna. Sources were read-only.

## Setup (first run, 30.10.2026)

- TVA `main` `239d01c` (GitHub #44). ThesisTester `4fc4af05` during the
  run.
- All eight day-path / sidecar flags on
  (`TVA_SIDECAR`, `TVA_PAUSE_GUARD`, `TVA_DAY_MANIFEST`,
  `TVA_EXCLUSIVE_FILLS`, `TVA_DAY_RULES`, `TVA_DAY_PUBLISH`,
  `TVA_TRADING_HOURS`, `TVA_NET_FEES`).
- Videos: NAS trading-videos tree for KW44, 18 mp4 files, each with a
  `<stem>.jsonl` sidecar. The trading-PC Videos folder had no KW44
  files, so nothing was copied from there.
- Trades: TopstepX lots export and TradesViz executions export for the
  same five days (42 lots / 84 fills).

## First-frame offset (first run)

Offsets were bimodal: 14 of 18 clips at 0.29–0.34 s (invalid under the
old single band 0.8–1.3 s) and 4 of 18 at 1.035–1.056 s (verified).
TVA rejected the 0.3 group and did not fall back silently. Root cause
(KW42–KW44, 55 complete sidecars): 44 low (0.23–0.40 s) and 11 high
(0.905–1.164 s). The jump sits on the start side; no sidecar field
predicts the group. Fix shipped as GitHub #45 (two bands).

The full-flag day path was not reached on this first run: `tva align`
aborted without a real OCR provider, `day build` failed
(`pause_checks` missing or key mismatch), `fills` then saw the day as
stale.

Independent sidecar-wall vs fill check: 50 of 84 fills in no clip, 0
fills in two clips. Per trade (42): 10 fully on video, 13 entry before
`record_start` with only the exit on video, 1 entry only, 18 not on
video at all. Typical case: entry a few seconds to 1.5 min before
Start.

Legacy variant B (`TVA_SIDECAR` only): 29 fills counted 2–4 times
(±30 min window). That double-count is what #46 `assign_fills_once`
fixes.

## Net (ThesisTester journal vs TopstepX)

On the first run TVA's own day-build net path did not run. The
ThesisTester journal (ingest-topstepx + positions) vs TopstepX per
trade was 42/42, 0 mismatches. TradesViz quantity and gross reconciled
on all 5 days.

| Day | Trades | Gross | Fees | Net |
|---|---|---|---|---|
| Mon 26 | 10 | 83.00 | 13.42 | 69.58 |
| Tue 27 | 9 | −247.50 | 17.08 | −264.58 |
| Wed 28 | 10 | −188.50 | 13.42 | −201.92 |
| Thu 29 | 8 | 150.50 | 10.98 | 139.52 |
| Fri 30 | 5 | 105.00 | 6.10 | 98.90 |
| Week | 42 | −97.50 | 61.00 | **−158.50** |

Fee used for the dry-run accounts file: 1.22 USD/RT MNQ (from the
TopstepX Fees + Commissions columns). The shipped
`src/tradevidanalyser/accounts.yaml` contains only synth accounts
(`synth-topstepx` MNQ 1.34, `synth-amp` MNQ 2.40).

## Tags / publish / flags (first run)

- No real KW44 tag proposals (no ASR / no transcripts on the box).
- Mechanics-only test (fake ASR/extract, variant B) produced mapped
  desk keys; unmapped playbook speech is reported loudly.
- Tag grammar `PB_`/`E_`/`V_` from 2026-11-02 was not in TVA on
  `239d01c` (shipped in #49).
- Rejected combinations: `TVA_NET_FEES` alone, `TVA_DAY_RULES` alone
  (`ungültige Flag-Kombination`). Allowed: `TVA_PAUSE_GUARD` alone,
  `TVA_SIDECAR` + `TVA_PAUSE_GUARD`, Vierer-Set + pause + manifest with
  or without `TVA_NET_FEES`.
- `tva align` on verified clips wrote `method=filename`, `offset_s 0.0`
  (alignment did not use the sidecar offset). #46 makes `tva align`
  read a verified `time_anchor.json`.

## Rerun after GitHub #46–#49

After `1128a6f` / `0b58d82` / `353c8a8` / `dbde7ae` (and the #45 two
bands):

- R-DLL equals the TopstepX net to the cent on all 5 days.
- 2026-10-27 and 2026-10-28 R-DLL violated.
- 11 on-video trades match.
- Publish correctly refused (no ThesisTester week lock: tick
  `prepare` + `lock_files`).

## Still true after the rerun

- Lots without video stay unassigned. Session-level net / evidence
  cover on-video trades only.
- Vienna calendar date and Topstep `TradeDay` /
  `trading_session_date` (ETH 18:00 America/New_York) differ after
  that NY cutoff — about 23:00 Vienna in this late-October DST gap,
  otherwise about midnight Vienna.
- `PB_ODG` is not rewritten to `PB_OGB`.
- The grammar validator is a copy of the TT test helper at
  `4fc4af05`. In-repo check:
  `test_grammar_errors_match_tt_reference` (inlined cases). Live TT
  map parity: `test_shipped_tag_map_matches_thesistester`.
