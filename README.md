# TradeVidAnalyser

Headless, bot-first service that turns recorded futures trading sessions
(OBS screen + spoken commentary, **German**) into a time-stamped **Session
Record** and a small **HTTP API** the desk's Grok bots can query — without
anyone opening a video.

v1 answers: *what was said on the tape?* It does **not** join ThesisTester
or import fills. TradesViz (AMP + TopstepX) and the lab are a later phase.

Series code **TVA**. Locked decisions: [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md).

## Install

Python 3.11+, `ffmpeg` / `ffprobe` on PATH.

| Host | Install | `TVA_ASR_PROVIDER` | Notes |
|---|---|---|---|
| CI / first-run | `pip install -e ".[dev]"` | `fake` (must set) | Unset is an error (`TVA_ASR_PROVIDER fehlt`). No GPU, no model download, no API key |
| Trading PC OCR | `pip install -e ".[dev,ocr]"` | ASR unchanged; `TVA_OCR_PROVIDER=paddleocr` | Optional PaddleOCR extra. Unset OCR is an error; `fake` only if set explicitly |
| Trading PC (CUDA) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=float16`; install a CUDA torch wheel from pytorch.org if pip gave you CPU-only |
| Mac (CPU) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=int8` + a warning; slow. Prefer the PC GPU or hosted ASR |
| Mac hosted fallback | `pip install -e ".[dev]"` | `deepgram` | `DEEPGRAM_API_KEY`; uploads `audio/mic.opus` only. Scribe is reserved, not the PR-05 pick |

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # then set TVA_ASR_PROVIDER=fake for CI
# pip install -e ".[dev,whisperx]" # local WhisperX (PC CUDA or Mac CPU)
tva doctor
```

`tva doctor` is read-only. On ALTrading run it only after trading (B.5).
It reports autostart (`tva-watch` via `schtasks /Query`, list argv,
Windows only; skipped elsewhere) and config state (ffmpeg, `TVA_ROOT`,
GPU, keys, providers). This package never registers a scheduled task
and never starts at boot or login. Accumu disables a registered task
by hand (D7). The XML / plist templates are **nicht für ALTrading (B.5)**.

WhisperX and Deepgram are **opt-in**. Unset `TVA_ASR_PROVIDER` is an error
(`TVA_ASR_PROVIDER fehlt`). Set `fake` explicitly for CI (a warning is
emitted). Optional knobs: `TVA_ASR_MODEL`
(WhisperX `large-v3`, Deepgram `nova-3`), `TVA_ASR_BATCH_SIZE`,
`TVA_ASR_DEVICE`, `TVA_ASR_COMPUTE_TYPE`, `DEEPGRAM_API_KEY`.

Set `TVA_ROOT` to the shared store (Synology mount, or a local folder):

```bash
export TVA_ROOT=/Volumes/tradevid   # Mac
# setx TVA_ROOT Z:\tradevid         # Windows, then new terminal (D8: no T:)
```

If unset, the CLI uses `./.tva_store` under the current directory.

## Commands

```bash
tva ingest "2026-09-11 14-30-00.mp4"
tva ingest "2026-09-11 14-30-00.mp4" --desktop-track
tva transcribe 2026-09-11_143000
tva extract 2026-09-11_143000
tva run --latest
tva run "2026-09-11 14-30-00.mp4" --desktop-track
tva status
tva doctor
tva watch --source /path/to/obs --once
tva wer 2026-09-11_143000 --ref path/to/reference.txt
tva frames 2026-09-11_143000 --contact-sheet
tva ocr 2026-09-11_143000
tva clips 2026-09-11_143000 --redact
tva vlm 2026-09-11_143000
tva fills 2026-09-11_143000 --executions executions.csv --venue topstepx
tva fills 2026-09-11_143000 --executions executions.csv --venue amp --reconcile-dir /path/to/journal
tva align 2026-09-11_143000
tva align 2026-09-11_143000 --manual-offset 1.8
tva evidence 2026-09-11_143000
tva rules 2026-09-11_143000
tva context 2026-09-11_143000 --lab-dir /path/to/journal
tva report 2026-09-11_143000
tva ledger add 2026-09-11_143000
tva rollup --week
tva rollup --week --month
tva publish 2026-09-11_143000 --notion
tva proposals 2026-09-11_143000
tva proposals confirm T01
tva coach --weeks 4
tva serve --host 127.0.0.1 --port 8764
```

OBS filenames must be `%CCYY-%MM-%DD %hh-%mm-%ss`. Record to the trading PC
disk, then `tva watch --source <obs dir>` copies into `$TVA_ROOT/recordings/`
(or pass the path to `tva ingest`). Watch never deletes the source and
never records onto the NAS. With `TVA_SIDECAR=1` (default off), watch and
ingest also copy `<stem>.jsonl` next to the recording (not `.events.jsonl`)
into `$TVA_ROOT/recordings/`. Ingest then writes
`sessions/<id>/time_anchor.json` (`schema_version "1"`): measured
first-frame offset per clip as wall−video (Q-15 = B; TT:439–440).
Validity is two bands (GitHub #45): low 0.20–0.45 s and high
0.80–1.30 s, stamped as `offset_mode: low|high`. The old single band
0.8–1.3 s is superseded. 1.043 s is only a loud fallback
(`offset_source: fallback` plus a warning on every clip). Flag off
does not write the file. The dest is `--root` / `TVA_ROOT`, never a
hardcoded NAS path. Watch is not an autostart: nothing in this
package registers `tva-watch` or starts it at boot or login. OBS auto-split
siblings (same filename prefix, next start within 5 s of the previous
duration) stitch into one session. `--desktop-track` writes
`audio/desktop.opus` and is never transcribed by default.

`tva wer` prints JSON `{wer, jargon_recall, n_words}` against a
hand-corrected reference (the golden excerpt on the NAS). It uses
[`docs/GLOSSARY.md`](docs/GLOSSARY.md) for jargon tokens.

`tva frames <id>` writes JPEGs at chapter markers ± 0/2/5 s (or `--at t…`)
to `$TVA_ROOT/sessions/<id>/frames/`. `--contact-sheet` adds
`frames/contact_sheet.jpg` so ROIs in [`layout.yaml`](layout.yaml) can be
measured. Raw frames stay under `TVA_ROOT` (PII).

`tva ocr <id>` reads those frames through `layout.yaml` ROIs and writes
`ocr.parquet` (`t, roi, text, confidence, parsed`). Clock parses to ISO
time, P&L to a signed float, position to an int. Fake OCR is the default
(`TVA_OCR_PROVIDER=fake`); `pip install 'tradevidanalyser[ocr]'` +
`TVA_OCR_PROVIDER=paddleocr` is opt-in and never required in CI.

`tva clips <id>` writes chapter windows (marker −30 s … +60 s) as
`clips/<t>.mp4` with the mic track only (`-c copy`). `--redact` applies
`layout.yaml` `*_mask` ROIs as black boxes (re-encodes video). The same
masks are burned into every JPEG `tva frames` writes.

`tva vlm <id>` is **off unless `TVA_VLM_PROVIDER` is set** (not even fake
by default). Input is redacted frames and/or clips — never the raw
recording. Grok sends JPEG `image_url` data URLs (`docs.x.ai` image
understanding; Imagine `video_url` is generation, not used). Gemini can
upload a redacted clip via the File API. Notes are stored on
`insights.visual_notes` (`frames_cited[]`) and `visual_notes.json`; a
note that states a number absent from `ocr.parquet` is dropped. Cost is
added to `status.json` `cost_usd`.

`tva fills <id> --executions <csv> --venue topstepx|amp` writes
`fills.parquet` / `trades.parquet` for fills whose UTC instant falls in
the session window (start − 30 min … end + 30 min). ThesisTester is used
when `pip install 'tradevidanalyser[journal]'`; otherwise the committed
`fills_mirror` path runs (CI). Zero fills omit the stage (not an error).
`--include-manual` pairs non-future rows. `commission` / `fees` are
discarded (TJ1). `--reconcile-dir` reads ThesisTester `reconcile.json`
and adds `recon_status` on `trades.parquet` (no AMP PDF parsing).

`tva align <id>` writes `session.alignment` (`offset_s`, `drift_s_per_h`,
`confidence`, `method`, `samples`). The prior is the OBS filename /
`start_wallclock_vienna`; the measurement is `ocr.parquet` clock rows.
Theil–Sen fits offset and drift; no usable clocks fall back to
`method=filename` at `confidence=0.6`. `--manual-offset s` records
`method=manual`. Not a bot `POST /run` stage.

`tva evidence <id>` maps each `trades.parquet` row to video time via
alignment and writes `evidence.json`: window (entry −180 s … exit +120 s,
`TVA_EVIDENCE_PRE_S` / `TVA_EVIDENCE_POST_S`), commentary seg ids, cited
`stated` fields (`setup`, `bias`, `stop_raw`, `target_raw`, `playbook`),
nearest frames/ocr/clip, and `alignment_confidence`. Confidence below 0.8
sets `alignment: low`. A trade with no speech keeps `stated` all null and
records a gap. Fake extract is the default; citations are required or
dropped. Not a bot `POST /run` stage.

`tva rules <id>` writes `rules.json` from `trades.parquet` (R-DLL,
R-MAX10, R-3L30, R-5M, R-REENTRY, R-CLOSE) and, when present,
`evidence.json` / `insights.session_events` (R-PLAYBOOK, R-DEFINED,
R-3C-CT, R-ARRIVAL, R-SLTP, R-HOURLY, R-ZONE, R-BIAS, R-TILT). Absent
speech is `unverifiable`, never `pass`. An evidence-backed `pass` always
cites a segment. R-SLTP stays `unverifiable` until a venue adapter can
see order modifications. Thresholds live in [`rules.yaml`](rules.yaml).
`daily_loss_limit_usd` is null (D4 parked) so R-DLL is `unverifiable`
until the desk sets it. PnL prefers `net_pnl_currency`, else
`gross_pnl_currency` with a `fees_unknown` note. Deterministic fills
rows never call a model; evidence-backed rows read already-written
files. Not a bot `POST /run` stage.

`tva context <id>` writes `context.json`: the day's Notion
`<D Mon YYYY> Macro Brief` / `NY session Brief` / `DRC ddmmyyyy` (bias,
conviction, kill levels quoted verbatim) plus ThesisTester
`attribution` / `zones` / `triggers` parquet from `--lab-dir` (or
`TVA_LAB_DIR`), joined on `entry_fill_id`. Missing brief → `brief: null`
+ a gap. Levels are never recomputed. Notion is fake by default
(`TVA_NOTION_PROVIDER=fake`); live read needs `NOTION_API_KEY` and
`TVA_NOTION_PROVIDER=notion`. Not a bot `POST /run` stage.

`tva report <id>` writes `debrief.md` / `debrief.json` in a fixed
section order (source · day · trades · rules · brief vs behaviour ·
observations · learnings · gaps). Facts come from files. Prose goes
through a `ReportProvider` (`fake` default, `grok` opt-in) that must
cite ids. Every digit run in `debrief.md` must already appear in
`trades.parquet`, `ocr.parquet`, or `context.json`. Not a bot
`POST /run` stage.

`tva ledger add <id>` upserts one session into `$TVA_ROOT/ledger/ledger.duckdb`
(tables `sessions`, `trades`, `rule_checks`, `events`). DuckDB reads
`trades.parquet` directly; rules / events / stated-vs-lab come from
`rules.json`, `insights.json`, `evidence.json`, and `context.json`.
Add is idempotent. `tva rollup --week [--month]` prints Markdown with
adherence rates, violation counts, trades/hour, and stated-vs-lab
agreement. `GET /ledger/summary?weeks=4` is the same math as JSON.
Optional omit-when-missing; not a bot `POST /run` stage.

`tva publish <id> --notion` is **off without `--notion`**. It writes or
updates one Trading Journal page titled `<D Mon YYYY> Session Debrief`
(tag *Trades Summary*; `Summaries` = one sentence; `Learning 1–3` from
the debrief) and prepends one line on *TVA runs*. Update is in place —
no duplicate pages. Notion is fake by default (`TVA_NOTION_PROVIDER=fake`);
live write needs `NOTION_API_KEY`, `TVA_NOTION_JOURNAL_DB`, and
`TVA_NOTION_PROVIDER=notion`. The bot may keep writing its own note.
Not a bot `POST /run` stage.

`tva proposals <id>` maps `stated.playbook` / `stated_levels` onto the
ThesisTester [`tag_map.yaml`](tag_map.yaml) vocabulary and writes
`intent_proposals.json` plus a TradesViz manual-import
`tradesviz_tags.csv` (`date, symbol, side, price, quantity, tags, notes`,
notes prefixed `[TVA proposed]`). Unknown spoken words are dropped, never
invented. `tva proposals confirm <id>` toggles `proposed` ↔ `confirmed`
(`T01` when unique, or `session_id:T01`). Optional omit-when-missing;
not a bot `POST /run` stage.

`tva coach --weeks 4` reads the DuckDB ledger plus the last N debriefs
and writes `coach/latest.json` / `coach/latest.md`. Each claim must cite
≥ N ledger row ids (`TVA_COACH_MIN_N`, default 10). At most one
experiment `{rule_change, start, stop_criterion}` is appended to
`ledger.experiments`. Fake coach is the default
(`TVA_COACH_PROVIDER=fake`); `grok` needs `XAI_API_KEY`.
`GET /coach/latest` is the bot read (404 until the CLI has run). Not a
`POST /run` stage.

ASR, extract, and OCR have **no silent fake default** (PR-43). Unset
`TVA_ASR_PROVIDER` / `TVA_EXTRACT_PROVIDER` / `TVA_OCR_PROVIDER` aborts with
`TVA_*_PROVIDER fehlt`. Set `fake` explicitly for CI (warning on stderr).
Report still defaults to **fake** (`TVA_REPORT_PROVIDER=fake`) so first-run
never calls a paid prose API.
WhisperX is the local adapter (`TVA_ASR_PROVIDER=whisperx`). Hosted fallback
is Deepgram (`TVA_ASR_PROVIDER=deepgram`, audio only). Grok extract is opt-in
(`TVA_EXTRACT_PROVIDER=grok`, `XAI_API_KEY`, optional `TVA_EXTRACT_MODEL`,
default `grok-4.6`). Grok debrief prose is the same key
(`TVA_REPORT_PROVIDER=grok`, optional `TVA_REPORT_MODEL`). Grok coach is
the same key (`TVA_COACH_PROVIDER=grok`, optional `TVA_COACH_MODEL`). The German
prompt lives in
[`prompts/insights_v1.de.md`](prompts/insights_v1.de.md).

## API (Grok)

```
GET /health
GET /sessions?days=7&status=missing,failed
GET /sessions/latest
GET /sessions/{id}
GET /sessions/{id}/status
GET /sessions/{id}/transcript
GET /sessions/{id}/insights
GET /sessions/{id}/clips/{name}     # 404 unless TVA_SERVE_MEDIA=1
GET /ledger/summary?weeks=4
GET /coach/latest
POST /sessions/{id}/run?stages=transcribe,extract   # 202, poll /status
```

`GET /sessions?days=7` keeps session ids whose filename date is within the
last 7 calendar days. `status` is a comma-separated stage state
(`ok|missing|failed|running`); a session matches if any stage is in that set.

`POST /sessions/{id}/run` starts `transcribe` / `extract` on a background
thread and sets `status.stages[x]=running`. Poll `GET /sessions/{id}/status`.
Committed response shapes live in [`examples/api/`](examples/api/).

Bind `tva serve` to localhost or a Tailscale interface. Off-loopback binds
(including `0.0.0.0`) require `TVA_API_TOKEN`; clients send
`Authorization: Bearer <token>`. Without a token those binds are refused
and requests get 401. Do not port-forward the API to the public internet.

```bash
export TVA_API_TOKEN=…          # required off-loopback
tva serve --host 127.0.0.1 --port 8764
# curl -H "Authorization: Bearer $TVA_API_TOKEN" http://mac:8764/sessions/latest
```

## Environment variables

Every `TVA_*` name the package reads. Unset/empty flag vars are off;
truth is `1` / `true` / `yes` / `on` (`flags.py`).

### Opt-in flags (default off)

| Variable | Default | Effect | Landed |
|---|---|---|---|
| `TVA_SIDECAR` | off | Orthogonal. Copy `<stem>.jsonl`; write `time_anchor.json`; sidecar-fallback fills/evidence. Does not license `TVA_NET_FEES`. | plan PR-39/40; GitHub #45/#46 |
| `TVA_PAUSE_GUARD` | off | Pause detector → `pause_checks`. Allowed alone or with sidecar. Required for the Vierer-Set. | plan PR-30 |
| `TVA_DAY_MANIFEST` | off | `tva day build` writes `days/<date>/`. Allowed alone or with pause. Required for the Vierer-Set. | plan PR-31 |
| `TVA_EXCLUSIVE_FILLS` | off | Exclusive fill ownership. Only with pause + manifest + the other three day-path flags. | plan PR-32 |
| `TVA_DAY_RULES` | off | Day-level rules + ledger. Same Vierer-Set. | plan PR-33 |
| `TVA_DAY_PUBLISH` | off | One Notion page per Vienna day. Same Vierer-Set. | plan PR-34 |
| `TVA_TRADING_HOURS` | off | Trades/hour on trading time. Same Vierer-Set. | plan PR-35 |
| `TVA_NET_FEES` | off | Net with costs from a TT journal dir. Only with the Vierer-Set. | plan PR-42 |
| `TVA_ALLOW_SESSION_ID_OVERWRITE` | off | Orthogonal. Loud session-id collision otherwise. | plan PR-36 |

The **Vierer-Set** is `TVA_EXCLUSIVE_FILLS` + `TVA_DAY_RULES` +
`TVA_DAY_PUBLISH` + `TVA_TRADING_HOURS`. Those four may only be on
together, and only with `TVA_PAUSE_GUARD` + `TVA_DAY_MANIFEST`.
Any other combination of the day-path / net-fees flags raises
`ungültige Flag-Kombination`.

### Other `TVA_*` vars

| Variable | Default | Effect |
|---|---|---|
| `TVA_ROOT` | `./.tva_store` | Store root. Overridden by `--root`. |
| `TVA_ACCOUNTS_FILE` | shipped synthetic `accounts.yaml` | Override path; missing fails loudly. Same as `--accounts-file`. |
| `TVA_DESK_RULES_FILE` | shipped `desk_rules.yaml` | Override path; missing fails loudly. Same as `--desk-rules-file`. |
| `TVA_API_TOKEN` | unset | Required for off-loopback `tva serve` (including `0.0.0.0`). |
| `TVA_SERVE_MEDIA` | off | `GET /sessions/{id}/clips/{name}` only when `1`/`true`/`yes`/`on`. |
| `TVA_ASR_PROVIDER` | unset (error) | `whisperx` / `deepgram` / explicit `fake`. Unset → `TVA_ASR_PROVIDER fehlt`. |
| `TVA_ASR_MODEL` | WhisperX `large-v3`, Deepgram `nova-3` | Model name. |
| `TVA_ASR_BATCH_SIZE` | provider default | WhisperX batch size (≥ 1). |
| `TVA_ASR_DEVICE` | auto | `cpu` / `cuda` / `cuda:N`. |
| `TVA_ASR_COMPUTE_TYPE` | auto | WhisperX compute type. |
| `TVA_ASR_VAD` | `silero` | WhisperX VAD method. |
| `TVA_ASR_USD_PER_HOUR` | unset | Hosted ASR cost hint. |
| `TVA_OCR_PROVIDER` | unset (error) | `paddleocr` / explicit `fake`. Unset → `TVA_OCR_PROVIDER fehlt`. |
| `TVA_EXTRACT_PROVIDER` | unset (error) | `grok` / explicit `fake`. Unset → `TVA_EXTRACT_PROVIDER fehlt`. |
| `TVA_EXTRACT_MODEL` | `grok-4.6` | Grok extract model. |
| `TVA_VLM_PROVIDER` | unset (off) | `fake` / `grok` / `gemini`. Unset leaves VLM off. |
| `TVA_VLM_MODEL` | provider default | VLM model override. |
| `TVA_REPORT_PROVIDER` | `fake` | `fake` / `grok`. |
| `TVA_REPORT_MODEL` | provider default | Grok debrief model. |
| `TVA_COACH_PROVIDER` | `fake` | `fake` / `grok`. |
| `TVA_COACH_MODEL` | provider default | Grok coach model. |
| `TVA_COACH_MIN_N` | `10` | Minimum ledger row ids per coach claim. |
| `TVA_NOTION_PROVIDER` | `fake` | `fake` / `notion`. |
| `TVA_NOTION_JOURNAL_DB` | unset | Trading Journal database id (live publish). |
| `TVA_NOTION_RUNS_PAGE` | unset | *TVA runs* page id. |
| `TVA_LAB_DIR` | unset | ThesisTester lab parquet dir (same as `--lab-dir`). |
| `TVA_EVIDENCE_PRE_S` | `180` | Evidence window seconds before entry. |
| `TVA_EVIDENCE_POST_S` | `120` | Evidence window seconds after exit. |
| `TVA_WATCH_STABLE_S` | `15` | `tva watch` mtime age (same as `--stable-seconds`). |
| `TVA_WATCH_POLL_S` | `2` | `tva watch` poll interval (same as `--interval`). |
| `TVA_LAYOUT` | shipped `layout.yaml` | Layout override. |
| `TVA_RULES` | shipped `rules.yaml` | Rules override. |
| `TVA_TAG_MAP` | shipped `tag_map.yaml` | Tag-map override. |

Related keys the code also reads (not `TVA_*`): `XAI_API_KEY`,
`DEEPGRAM_API_KEY`, `NOTION_API_KEY`, `GEMINI_API_KEY` /
`GOOGLE_API_KEY`, `ELEVENLABS_API_KEY`. Bot prompts use
`TVA_API_BASE` (URL only); the package does not read it.

## CLI flags

Global (every command):

| Flag | Effect |
|---|---|
| `--root PATH` | Store root (`TVA_ROOT`). |
| `--accounts-file PATH` | Sets `TVA_ACCOUNTS_FILE` for this process. |
| `--desk-rules-file PATH` | Sets `TVA_DESK_RULES_FILE` for this process. |
| `--json` | Machine-readable stdout. |
| `--version` | Print `tva` version. |

| Command | Flags |
|---|---|
| `ingest` | `video`, `--desktop-track` |
| `transcribe` | `session`, `--provider` |
| `extract` | `session`, `--provider` |
| `run` | `--latest`, `video`, `--desktop-track` |
| `watch` | `--source` (required), `--once`, `--run`, `--stable-seconds`, `--interval` |
| `wer` | `session`, `--ref` (required) |
| `frames` | `session`, `--at T…`, `--contact-sheet` |
| `ocr` | `session`, `--provider` |
| `clips` | `session`, `--redact` |
| `vlm` | `session`, `--provider` |
| `fills` | `session`, `--executions` (required), `--venue`, `--include-manual`, `--reconcile-dir`, `--positions-dir`, `--account` |
| `align` | `session`, `--manual-offset S`, `--force` (only while `TVA_PAUSE_GUARD` is on) |
| `evidence` | `session`, `--provider` |
| `rules` | `session` |
| `context` | `session`, `--lab-dir` |
| `report` | `session`, `--provider` |
| `day audit` | `--from`, `--to`, `--executions`, `--loader`, `--clock-note`, `--sample-clocks` |
| `day build` | `YYYY-MM-DD`, `--executions` (required), `--venue`, `--include-manual`, `--reconcile-dir`, `--provider`, `--positions-dir`, `--account` |
| `day enable` | optional `YYYY-MM-DD` |
| `day state` | `YYYY-MM-DD` |
| `day delete` | `YYYY-MM-DD` or `--all` |
| `pause-checks delete` | `session` or `--all` |
| `ledger add` | `session` |
| `ledger drop-day` | `YYYY-MM-DD` |
| `rollup` | `--week [YYYY-Www]`, `--month [YYYY-MM]` |
| `publish` | `session`, `--notion` (required to write), `--provider` |
| `proposals` | `session` or `confirm` + proposal id |
| `coach` | `--weeks N`, `--provider` |
| `serve` | `--host` (default `127.0.0.1`), `--port` (default `8764`) |

`status` and `doctor` take no extra flags.

## Bekannte Grenzen

- **Publish needs a TT week lock.** Under `TVA_NET_FEES`, `tva publish`
  calls `require_locked_week_manifest`: ThesisTester `week_manifest.json`
  must be written by `lock_files` after tick `prepare` /
  `prepare_manifest`, then verified (`validate_week_manifest`,
  `assert_manifest_inputs_match`, `assert_claimed_lock_supported`). A
  hand-edited `locked` file cannot unlock.
- **Vienna date vs Topstep `TradeDay`.** TVA day keys are the
  Europe/Vienna calendar date of the clip start. Fill
  `session_date` / Topstep `TradeDay` follow ThesisTester
  `trading_session_date` (ETH 18:00 America/New_York). Those dates
  differ for trades after about 23:00 Vienna.
- **`PB_ODG` is not canonicalised to `PB_OGB`.** `tag_map.yaml`
  registers `OGB` with `renamed_from: [ODG]`. The grammar validator
  treats `PB_ODG` as an alias of playbook `OGB`; `map_stated_text`
  still emits `PB_ODG`.
- **Grammar validator is a copy.** `tag_grammar.py` matches
  ThesisTester `tests/test_tag_map_schema.py::_grammar_errors` /
  `_ma_pair_ok` at pin `4fc4af05`. Parity:
  `tests/test_proposals.py::test_grammar_errors_match_tt_reference`.
- **Day net / evidence cover on-video trades.** Exclusive
  `trades.parquet`, evidence, and the day-rollup `trade_count` use
  assigned (on-video) trades. Lots whose timestamps fall in no clip
  core stay `outside` / unassigned (`trades_outside_clips`).
- **Old first-frame band superseded.** The single band 0.8–1.3 s
  (plan §3.3 / PR-40) is replaced by GitHub #45: 0.20–0.45 s and
  0.80–1.30 s.

## Tests

```bash
ruff check src tests
pytest -q
```

## Documents

| Doc | What it is |
|---|---|
| [`docs/01_DISCOVERY.md`](docs/01_DISCOVERY.md) | Desk as it is, where the gap is |
| [`docs/02_SCOPE.md`](docs/02_SCOPE.md) | Job, v1 vs later, principles |
| [`docs/03_STATE_OF_THE_ART.md`](docs/03_STATE_OF_THE_ART.md) | ASR / VLM research |
| [`docs/04_ARCHITECTURE.md`](docs/04_ARCHITECTURE.md) | NAS, API, Session Record |
| [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md) | TVA0–TVA3 and locked D1–D9 |
| [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) | Build contract: PR-00…PR-27, contracts, acceptance |
| [`docs/TVA_GROK_ROUTINE_PACK.md`](docs/TVA_GROK_ROUTINE_PACK.md) | Grok bot pack: API only, evening, watchdog, Sunday |
| [`examples/bot/`](examples/bot/) | Copy-ready `SYSTEM.md` + evening + Sunday routines |
| [`docs/GOLDEN_EXCERPT.md`](docs/GOLDEN_EXCERPT.md) | Cut, redact, hand-correct the WER reference |
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | German speech + English level tokens |
| [`CHANGELOG.md`](CHANGELOG.md) | Plan PR ↔ GitHub # ↔ merge commit |
| [`docs/GITHUB_PRS_45_49.md`](docs/GITHUB_PRS_45_49.md) | Landed GitHub #45–#49 |
| [`docs/INBETRIEBNAHME.md`](docs/INBETRIEBNAHME.md) | Commissioning plan (incl. #46–#49) |
| [`docs/KW44_DRYRUN.md`](docs/KW44_DRYRUN.md) | KW44 dry-run summary |
| [`docs/COACH_THESISTESTER_RULES.md`](docs/COACH_THESISTESTER_RULES.md) | Coach ↔ ThesisTester rules (2026-10-31) |
