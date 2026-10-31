# GitHub #45–#49 (landed on `main`)

Verified against `main` @ `dbde7ae` and the merge commits below. These
are **GitHub pull-request numbers**, not the Phase 2 plan labels
PR-45 / PR-46 in [`plans/phase-2-sidecar-facts.md`](plans/phase-2-sidecar-facts.md).
Plan PR-45 and plan PR-46 (coach v2 / R8) remain deferred.

Mapping: [`../CHANGELOG.md`](../CHANGELOG.md).

---

## #45 — two first-frame offset bands (`13e6892`)

**Commit:** `13e6892` *Accept two first-frame offset bands from KW42-44 evidence*
(2026-10-30). Plan amendment the same day (Accumu / Florian option A).

Under `TVA_SIDECAR`, `sidecar.py` measures the first-frame offset per
clip as `wall_span − video_span` (Q-15 = B). Validity is now two
measured bands:

| Mode | Band | `time_anchor.json` |
|---|---|---|
| `low` | 0.20–0.45 s | `offset_mode: low` |
| `high` | 0.80–1.30 s | `offset_mode: high` |

Values in the gap (0.45–0.80 s) or outside both bands fail loudly
(`first_frame_offset_out_of_band`). `record_start` without
`record_stop` is `incomplete` (warning, no fallback offset). The
1.043 s fallback stays loud (`offset_source: fallback`) when the
measure itself fails. Flag-off output is unchanged. CI pin stays
ThesisTester `4fc4af05`.

The old single band **0.8–1.3 s** (plan §3.3 / PR-40) is
**superseded** by these two bands. Historical aliases
`FIRST_FRAME_OFFSET_MIN_S` / `MAX_S` still point at the high band
only.

---

## #46 — sidecar-fallback fixes (`1128a6f`)

**Commit:** `1128a6f` *Fix KW44 sidecar-fallback windows, fill
double-count, fees, and CLI* (2026-10-31). Behind `TVA_SIDECAR`
unless noted. Flag-off output and the CI pin stay unchanged.

| Fix | What the code does |
|---|---|
| Fill-once | `assign_fills_once`: each fill is assigned to at most one session. Prefer the unpadded clip that contains it; pad-only hits go to the nearest unique clip so isolated sessions keep the ±30 min window. Used when sidecar is on and exclusive fills is off. |
| Fees kept with TT installed | ThesisTester's import loader drops TV `commission` / `fees`. Sidecar fallback overlays those costs from the mirror CSV parse (`_overlay_csv_costs`) and sets `net = gross − (commission + fees)` (`_apply_csv_fees`) on both import and mirror loaders. Under `TVA_NET_FEES` the journal path already keeps costs (missing costs → `Netto ohne Kostenquelle`). |
| Full rollback on abort | `_discard_failed_day_build` restores `current`, session fills/status, and day artifacts. An aborted `tva day build` writes neither `days/enabled_from` nor empty day folders. |
| Real TT reconcile statuses | `--reconcile-dir` accepts the ThesisTester `4fc4af05` day-wide `reconcile.json` (`session_date` + instrument). Statuses: `reconciled`, `journal_missing`, `amp_missing`, `topstepx_missing`, `multiset_mismatch`, `pnl_mismatch`. Unknown statuses fail closed. (`fee_mismatch` is #47.) |
| No-video metric without padding | Off-camera trades use the raw span (no ±180/120 s pad, no `[duration, duration]` clamp). `evidence.json` records `no_video_coverage` / `no_video_trades` and gap `no video/no speech`. Gated behind `TVA_SIDECAR` so flag-off evidence stays identical. |
| Accounts override | `TVA_ACCOUNTS_FILE` / `--accounts-file`: set reads that path; missing fails loudly; unset keeps the shipped synthetic `accounts.yaml`. |

Also in this merge: `tva day audit` accepts a TT journal directory
under `TVA_NET_FEES`; `tva align` uses a verified `time_anchor.json`
(`method=sidecar`, `confidence=1.0`).

---

## #47 — `fee_mismatch` and desk-rules path (`0b58d82`)

**Commit:** `0b58d82` *Accept TT fee_mismatch and add desk_rules.yaml
path override* (2026-10-31).

- `fee_mismatch` is in `RECON_STATUSES` (`fills.py`). ThesisTester
  `4fc4af05` `reconcile.json` is accepted; unknown statuses still fail
  closed.
- `TVA_DESK_RULES_FILE` / `--desk-rules-file` mirror the accounts
  override: set reads that path; missing fails loudly; unset keeps the
  shipped `desk_rules.yaml`.

---

## #48 — day rules and ownership see only that day's trades (`353c8a8`)

**Commit:** `353c8a8` *Filter journal trades to the build day before
ownership and rules* (2026-10-31).

`assign_day_ownership` used to pass the full journal into `pair_fills`
/ `apply_day_rules`, so R-DLL, R-MAX10, R-3L30 and
`trades_outside_clips` saw every day. It now keeps journal trades
whose entry fill is a candidate for the build day — the same
Vienna/core set as day fills (`journal_trades_for_day`). Flag-off
and CSV pairing are unchanged.

---

## #49 — TT tag grammar after 2026-11-02 (`dbde7ae`)

**Commit:** `dbde7ae` *Map JX0-A1 PB_/E_/V_/T_/C_ grammar from TT
4fc4af05 after 2026-11-02* (2026-10-31).

Spoken mapping stays JX0b-only before the cutoff or with `as_of`
unset. Closed prefixed tags and `ma_pair` variants map on/after
`tag_grammar.effective_from` (`2026-11-02`). The gate uses each
trade's `session_date` (else the session-id date).

Shipped `tag_map.yaml` (repo root and
`src/tradevidanalyser/tag_map.yaml`) appends TT's A1 registries with
`notion_url` stripped. `tests/test_proposals.py::test_grammar_errors_match_tt_reference`
is the parity check against the TT helper at `4fc4af05`.

| Rule | Value |
|---|---|
| Prefixes | `PB_`, `E_`, `V_`, `T_`, `C_none` |
| Reserved | `PB_`, `E_`, `V_`, `T_`, `C_`, `X_` |
| `X_` | Retired (D14): no valid values (`unknown_reserved`) |
| Cardinality | `PB_=1`, `E_=1`, `V_=0..1` (also `T_=0..1` in YAML) |
| Entry | `E_agg`, `E_kon` |
| Confluence none | `C_none` (the only valid `C_` token) |
| `ma_pair` | YAML `variant_patterns.ma_pair` (1m / 5m / 30m, EMA or SMA, `len1 < len2`; both legs must be confirm desk keys) |
| Per-playbook variants | e.g. `PB_3050`: `V_FR`/`V_SFP`/`V_FA`; `PB_MAC`: `ma_pair` only |
| OGB | registered as a rename of ODG (`renamed_from: [ODG]`). `PB_ODG` is accepted by the validator as an alias of `OGB`; mapping does **not** rewrite `PB_ODG` → `PB_OGB` |

The grammar validator in `tag_grammar.py` is copied from ThesisTester
`tests/test_tag_map_schema.py::_grammar_errors` / `_ma_pair_ok` at
`4fc4af05`.
