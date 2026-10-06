# L0 parity snapshot (`main` @ `f44d864`)

Synthetic CI fixture for plan §3.1. No real session, no account data, no NAS.

Two one-clip runs of the pipeline as of Coach (PR-27 / `f44d864`):

- `l0-ocr/` — seconds-clock Theil–Sen fit (`alignment.method = ocr_clock`)
- `l0-filename/` — no clock rows, filename fallback

Inputs (regenerate only to rebuild this snapshot): `inputs/2026-05-14 16-03-00.mp4`, `inputs/executions.csv`, `inputs/ocr.parquet` (L0-ocr only).

Compared later (flags on) against these artifacts:

- `session.json`, `fills.parquet`, `trades.parquet`, `evidence.json`, `rules.json`
- `debrief.md`, `debrief.json`
- `ledger.json` (session / trades / rule_checks / events rows)
- `notion_payload.json` (title, summaries, learnings)

`days/` must not exist on L0. `pause_checks/` is not part of byte equality.

Compare: parquet via `pyarrow.Table.equals(..., check_metadata=False)` plus matching columns and types. JSON canonical (sorted keys) after dropping exactly `app_version`, publish `log_line`, and publish `created`.
