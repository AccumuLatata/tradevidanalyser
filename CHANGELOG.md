# Changelog

Series numbers (PR-00…PR-46) come from
[`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md),
[`docs/plans/multi-clip-day.md`](docs/plans/multi-clip-day.md), and
[`docs/plans/phase-2-sidecar-facts.md`](docs/plans/phase-2-sidecar-facts.md).
They are **not** GitHub pull-request numbers. GitHub `#N` is the
`( #N )` merge on `main`. Hashes are those merge commits.

This file starts at the KW44 docs catch-up. Earlier rows are the
already-merged series, reconstructed from `git log` on `main`.

---

## Unreleased (docs only)

- Docs for GitHub #45–#49, env/flag table, Bekannte Grenzen,
  commissioning plan, KW44 dry-run summary, Coach↔ThesisTester rules.
- Review pass against `main` @ `dbde7ae`: sidecar align/fees/evidence,
  OCR/extract unset, WhisperX batch-size default, grammar test names,
  exclusive offset-band gap, Vienna vs NY cutoff, 0.026 s [Rv] vs
  0.10 s code gate.
- No code, tests, or goldens.

---

## GitHub #45–#49 (2026-10-30 / 2026-10-31)

These GitHub numbers are **not** plan PR-45 / PR-46.

| GitHub | Merge | Flag / surface | What landed |
|---|---|---|---|
| [#45](https://github.com/AccumuLatata/tradevidanalyser/pull/45) | `13e6892` | `TVA_SIDECAR` (already shipped) | Two first-frame bands 0.20–0.45 s and 0.80–1.30 s; `offset_mode: low\|high`. Old single band 0.8–1.3 s superseded. |
| [#46](https://github.com/AccumuLatata/tradevidanalyser/pull/46) | `1128a6f` | `TVA_SIDECAR`; `TVA_ACCOUNTS_FILE` / `--accounts-file` | Sidecar-fallback: fill-once, fees kept with TT installed, full rollback on abort, real TT reconcile statuses, no-video metric without padding. |
| [#47](https://github.com/AccumuLatata/tradevidanalyser/pull/47) | `0b58d82` | `TVA_DESK_RULES_FILE` / `--desk-rules-file` | `fee_mismatch` in `RECON_STATUSES`; desk-rules path override. |
| [#48](https://github.com/AccumuLatata/tradevidanalyser/pull/48) | `353c8a8` | day path (`TVA_DAY_RULES` / exclusive fills) | Journal trades filtered to the build day before ownership and rules. |
| [#49](https://github.com/AccumuLatata/tradevidanalyser/pull/49) | `dbde7ae` | proposals / `tag_map.yaml` | JX0-A1 `PB_`/`E_`/`V_`/`T_`/`C_none` grammar from TT `4fc4af05`, effective 2026-11-02, gated by `session_date`. |

Detail: [`docs/GITHUB_PRS_45_49.md`](docs/GITHUB_PRS_45_49.md).

---

## Phase 2 plan series (PR-37…PR-42) and follow-ups

| Plan | GitHub | Merge | Flag | Note |
|---|---|---|---|---|
| PR-37 | [#36](https://github.com/AccumuLatata/tradevidanalyser/pull/36) | `674a9bf` | — (docs) | Phase 2 sidecar facts plan. |
| PR-38 | [#37](https://github.com/AccumuLatata/tradevidanalyser/pull/37) | `4191dd5` | — | Autostart guard; read-only `tva doctor`. |
| PR-43 | [#38](https://github.com/AccumuLatata/tradevidanalyser/pull/38) | `be7defa` | none (default) | N4: no silent provider fallbacks. |
| PR-39 | [#39](https://github.com/AccumuLatata/tradevidanalyser/pull/39) | `ee0bcc0` | `TVA_SIDECAR` | Copy `<stem>.jsonl`. |
| PR-40 | [#40](https://github.com/AccumuLatata/tradevidanalyser/pull/40) | `b314172` | `TVA_SIDECAR` | Sidecar reader + `time_anchor.json`. First-frame band then 0.8–1.3 s; superseded by GitHub #45. |
| PR-42 | [#41](https://github.com/AccumuLatata/tradevidanalyser/pull/41) | `e9dc340` | `TVA_NET_FEES` | Facts mirror; Vierer-Set only. |
| — | [#42](https://github.com/AccumuLatata/tradevidanalyser/pull/42) | `7fe18af` | — | CI journal job; PR-40 `[Rv]` first-frame goldens. |
| — | [#43](https://github.com/AccumuLatata/tradevidanalyser/pull/43) | `c7e4aae` | — | Drop leftover `[Rv]` skip placeholders; journal skip guard. |
| — | [#44](https://github.com/AccumuLatata/tradevidanalyser/pull/44) | `239d01c` | — | CI TT pin `4fc4af05`; desk-key test isolation; PR-40 section residuals. |
| PR-41 | — | — | — | Deferred (plan). |
| PR-44 | — | — | — | Deferred; only if Deepgram is used (plan). |
| PR-45 | — | — | — | Deferred (plan). **Not** GitHub #45. |
| PR-46 | — | — | — | Deferred: coach v2 / R8 contract (plan). **Not** GitHub #46. |

---

## Phase 1 plan series (PR-28…PR-36)

| Plan | GitHub | Merge | Flag |
|---|---|---|---|
| PR-28 (plan docs) | [#26](https://github.com/AccumuLatata/tradevidanalyser/pull/26) | `3d01983` | — (docs: `multi-clip-day.md`) |
| PR-28 (audit) | [#27](https://github.com/AccumuLatata/tradevidanalyser/pull/27) | `282a19e` | — (`tva day audit`) |
| PR-29 | [#28](https://github.com/AccumuLatata/tradevidanalyser/pull/28) | `a161fb7` | — (invalid alignment literal) |
| PR-30 | [#29](https://github.com/AccumuLatata/tradevidanalyser/pull/29) | `0ed113c` | `TVA_PAUSE_GUARD` |
| PR-31 | [#30](https://github.com/AccumuLatata/tradevidanalyser/pull/30) | `a03ddd6` | `TVA_DAY_MANIFEST` |
| PR-32 | [#31](https://github.com/AccumuLatata/tradevidanalyser/pull/31) | `104ae9d` | `TVA_EXCLUSIVE_FILLS` |
| PR-33 | [#32](https://github.com/AccumuLatata/tradevidanalyser/pull/32) | `f2411f7` | `TVA_DAY_RULES` |
| PR-34 | [#33](https://github.com/AccumuLatata/tradevidanalyser/pull/33) | `1167e15` | `TVA_DAY_PUBLISH` |
| PR-35 | [#34](https://github.com/AccumuLatata/tradevidanalyser/pull/34) | `6fc8009` | `TVA_TRADING_HOURS` |
| PR-36 | [#35](https://github.com/AccumuLatata/tradevidanalyser/pull/35) | `c6029b8` | `TVA_ALLOW_SESSION_ID_OVERWRITE` |

The plan file says the earlier number PR-34 (audit) was renumbered to
PR-28. GitHub #26 is the Phase 1 **plan**; GitHub #27 is the audit
implementation that the commit subject still calls PR-28.

---

## Implementation-plan series (PR-00…PR-27)

| Plan | GitHub | Merge | Intent |
|---|---|---|---|
| PR-00 | — | `d37d0fd` | Plan lock (docs 01–05, D1–D9). No GitHub PR on `main`. |
| PR-01 | — | `8f1f61f` | Skeleton: package, CLI, store, fake providers, API. No GitHub PR on `main`. |
| PR-02 | — | `a7fb34e` | Implementation plan + routine-pack stub. No GitHub PR on `main`. |
| PR-03 | [#1](https://github.com/AccumuLatata/tradevidanalyser/pull/1) | `10d1ef8` | Glossary + WER harness. |
| PR-04 | [#2](https://github.com/AccumuLatata/tradevidanalyser/pull/2) | `574b005` | WhisperX adapter. |
| PR-05 | [#3](https://github.com/AccumuLatata/tradevidanalyser/pull/3) | `4762fee` | Hosted Deepgram ASR. |
| PR-06 | [#4](https://github.com/AccumuLatata/tradevidanalyser/pull/4) | `fcb4609` | `tva watch` copy-after-record. |
| PR-07 | [#5](https://github.com/AccumuLatata/tradevidanalyser/pull/5) | `2f13a81` | OBS split-file stitch + desktop track. |
| PR-08 | [#6](https://github.com/AccumuLatata/tradevidanalyser/pull/6) | `db408fc` | Grok extractor. |
| PR-09 | [#7](https://github.com/AccumuLatata/tradevidanalyser/pull/7) | `a334704` | Session events + summaries. |
| PR-10 | [#8](https://github.com/AccumuLatata/tradevidanalyser/pull/8) | `e4e14a0` | API hardening (`TVA_API_TOKEN`). |
| PR-11 | [#9](https://github.com/AccumuLatata/tradevidanalyser/pull/9) | `411eca2` | Grok bot routine pack. |
| PR-12 | [#10](https://github.com/AccumuLatata/tradevidanalyser/pull/10) | `5a3f7c9` | `layout.yaml` + chapter keyframes. |
| PR-13 | [#11](https://github.com/AccumuLatata/tradevidanalyser/pull/11) | `ca1f2d8` | ROI OCR. |
| PR-14 | [#12](https://github.com/AccumuLatata/tradevidanalyser/pull/12) | `e93ddbf` | Redact + chapter clips. |
| PR-15 | [#13](https://github.com/AccumuLatata/tradevidanalyser/pull/13) | `1cde160` | Opt-in VLM notes. |
| PR-16 | [#14](https://github.com/AccumuLatata/tradevidanalyser/pull/14) | `613dd54` | TradesViz fills loader. |
| PR-17 | [#15](https://github.com/AccumuLatata/tradevidanalyser/pull/15) | `59e6db6` | AMP `recon_status` passthrough. |
| PR-18 | [#16](https://github.com/AccumuLatata/tradevidanalyser/pull/16) | `04a5d89` | OCR clock alignment. |
| PR-19 | [#17](https://github.com/AccumuLatata/tradevidanalyser/pull/17) | `63b76a5` | Evidence windows. |
| PR-20 | [#18](https://github.com/AccumuLatata/tradevidanalyser/pull/18) | `0737e7f` | Deterministic rule scorecard. |
| PR-21 | [#19](https://github.com/AccumuLatata/tradevidanalyser/pull/19) | `c4676f7` | Evidence-backed rules. |
| PR-22 | [#20](https://github.com/AccumuLatata/tradevidanalyser/pull/20) | `92a8ca2` | Context join. |
| PR-23 | [#21](https://github.com/AccumuLatata/tradevidanalyser/pull/21) | `8dfe599` | Session debrief report. |
| PR-24 | [#22](https://github.com/AccumuLatata/tradevidanalyser/pull/22) | `64aaf3e` | DuckDB ledger + rollup. |
| PR-25 | [#23](https://github.com/AccumuLatata/tradevidanalyser/pull/23) | `fd04ae6` | Optional Notion publish. |
| PR-26 | [#24](https://github.com/AccumuLatata/tradevidanalyser/pull/24) | `af6a311` | Intent proposals. |
| PR-27 | [#25](https://github.com/AccumuLatata/tradevidanalyser/pull/25) | `f44d864` | Coach. |

From PR-22 onward the merge on `main` is the squash commit that names
both the plan number and the GitHub number. Earlier rows use the
`Merge pull request #N` commit.
