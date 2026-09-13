# TradeVidAnalyser — Grok Bot routine pack

**Status:** landed (PR-11). A fresh Grok bot given **only this pack** (plus
the three files under `examples/bot/`) can complete the evening routine
against a real session.
**Plan:** [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) §5 PR-11.
**Copy-ready prompts:** [`examples/bot/SYSTEM.md`](../examples/bot/SYSTEM.md),
[`ROUTINE_EVENING.md`](../examples/bot/ROUTINE_EVENING.md),
[`ROUTINE_SUNDAY_AUDIT.md`](../examples/bot/ROUTINE_SUNDAY_AUDIT.md).
**Response shapes:** [`examples/api/`](../examples/api/).

TradeVidAnalyser does **not** embed Grok, host a queue, or ship an MCP
server. The bot talks **HTTP only** to `tva serve` on the always-on Mac.
Quiet on success. One log line. Never invent numbers or quotes.

---

## 0. Install this pack

1. Create (or reuse) a Grok bot. Paste `examples/bot/SYSTEM.md` as the
   **system** instruction. Do not also paste LAN IPs, `TVA_ROOT`, or the
   raw token into the system text.
2. Store two secrets in the bot’s secret store (same convention as other
   desk bots): `TVA_API_BASE` (Tailscale or tunnel **URL**, never a LAN
   IP) and `TVA_API_TOKEN`.
3. Schedule `examples/bot/ROUTINE_EVENING.md` after the NY session
   (desk default: when the tape is expected on the NAS).
4. Schedule `examples/bot/ROUTINE_SUNDAY_AUDIT.md` on Sunday.
5. Schedule the **23:30 Europe/Vienna** watchdog (same evening file,
   “Watchdog” section).

The bot already owns its Notion pages (session note + *TVA runs* log).
TVA does not create Notion databases.

---

## 1. Surfaces (API only)

Every call: `Authorization: Bearer $TVA_API_TOKEN`.
Base: `$TVA_API_BASE` (no trailing slash). Off-loopback without a token
is **401**.

| Surface | When | Notes |
|---|---|---|
| `GET /health` | start of every routine | Use **only** `ok`. `ok: false` / HTTP fail / 401 → one chat ping, stop. Never copy `root` (that is `TVA_ROOT`) |
| `GET /sessions?days=1` | watchdog | today only (`age < 1` Vienna calendar days) |
| `GET /sessions?days=7` | Sunday audit | last 7 calendar days including today (`0 ≤ age < 7`) |
| `GET /sessions?days=7&status=missing,failed` | Sunday shortcut | session matches if **any** stage is in the set. In-flight `running` does **not** match `missing` |
| `GET /sessions/latest` | evening | bundle: `session`, `status`, `transcript`, `insights`. **404** if the store is empty |
| `GET /sessions/{id}` | same bundle for a known id | 404 if unknown |
| `GET /sessions/{id}/status` | after `POST …/run` | poll until no stage is `running` |
| `GET /sessions/{id}/insights` | on demand | **404** if `insights.json` is absent (bundle uses `null`) |
| `GET /sessions/{id}/transcript` | to verify a quote | **404** if absent; **only** source of quotes |
| `POST /sessions/{id}/run?stages=transcribe,extract` | extract is `missing`, ingest is `ok`, nothing is `running` | **202** `{accepted, session_id, stages, status}`. **409** = already running → poll, do not POST again |
| `GET /ledger/summary?weeks=4` | on demand | trailing ISO weeks: adherence, violations, trades/hour, stated-vs-lab. Empty ledger → zeros, not 404 |

**Do not call:** CLI (`tva …`), NAS paths, `ffmpeg`, video files,
`GET /sessions/{id}/clips/…` (gated; TVA3). Do not `POST` any other path.

The bot never mounts the NAS, never runs `ffmpeg`, never opens a video.

`GET /sessions` list body is `{sessions: [id…], latest}`. `latest` is the
newest id **in that filtered list**, not a global latest. Ids are sorted
oldest-first (`YYYY-MM-DD_HHMMSS`). Prefer the `/sessions/latest` bundle
over `/transcript` or `/insights` when those files may be missing.

---

## 2. What the JSON means

`GET /sessions/latest` (see `examples/api/latest.json`):

| Key | Meaning |
|---|---|
| `session.id` | `YYYY-MM-DD_HHMMSS` (Vienna clock from the OBS filename) |
| `session.recording` | path, hash, duration, chapters, optional `parts[]` — do not copy `path` into Notion |
| `status.stages` | `ingest` / `transcribe` / `extract` → `ok\|missing\|failed\|running` |
| `status.error` | last durable stage error, or `null` when no stage is `failed` |
| `transcript` | object or `null` if transcribe has not written `transcript.json` |
| `transcript.segments` | **array** of `{id, t0, t1, lang, text, words}` — `id` is `seg_NNN` |
| `insights` | cited extraction object, or `null` if extract has not written `insights.json` |

`running` is a **live overlay** while `POST /run` is in flight. A stage
that is already `ok` stays `ok`. A durable `failed` shows as `running`
during a retry, then `failed` again if the retry dies. Poll the API;
do not assume on-disk `status.json`.

`extract == ok` if and only if the bundle’s `insights` is a non-null
object (lists may be empty). `insights: null` means extract has not
succeeded yet (`missing`, `failed`, or `running`).

Insights fields (each span is `{seg, text, t?, name?, token?, raw_text?}`
except `gaps` which are strings):

`bias_statements`, `playbooks_mentioned`, `stated_levels`,
`stated_stops_targets`, `checkins`, `tilt_markers`, `brief_refs`,
`observations`, `gaps`, `session_events[{t, kind, seg, text}]`,
`summary_de`, `summary_en`.

`kind` is a closed set: `hourly_checkin`, `bias_statement`,
`no_trade_zone`, `trade_zone`, `tilt`, `break`, `rule_mention`,
`brief_ref`, `grok_ref`.

`provider` / `model` / `prompt_version` live on `transcript` and
`insights`, not on `status`.

**Quotes:** `transcript.segments` is a list, not a dict. Find the
segment whose `id` equals `span.seg` (or `event.seg`). `text` must be a
verbatim substring of that segment’s `text`. If no segment matches, or
the quote is not a substring, drop it — do not “fix” it. Same rule for
`raw_text` when you quote it.

---

## 3. Hard rules

| Forbidden | Why |
|---|---|
| Invent a quote, or paraphrase a quote as if it were verbatim | citations are the trust model |
| Quote text that is not a substring of its `/transcript` segment | every quote must exist in `/transcript` |
| Treat a spoken number / price / “Ziel 18k” as a fill, P&L, or count | numbers come from fills/OCR later; speech stays `raw_text` |
| Call anything but this API | CLI is for the scheduler on the LAN |
| Store or print a LAN IP, the API token, or `TVA_ROOT` | secrets and topology stay out of Notion and chat |
| Copy `GET /health` → `root` (or any check `detail` that looks like a path) | that field **is** `TVA_ROOT` |
| Put `TVA_API_TOKEN` in a page, log line, or screenshot | token is a secret |
| Edit `insights.json` / `transcript.json` (or ask the API to) | facts are files the app owns |
| Report `unverifiable` as pass or fail | it means “no evidence”, nothing more |
| Claim TVA3–TVA7 outputs exist | see §7 |
| Write a note or a success log line for a session whose id is not **today** (Europe/Vienna) | that silences the 23:30 watchdog |

Quiet on success: if stages are `ok` and a note was written, **do not**
ping chat. One log line on *TVA runs* is enough.

---

## 4. Evening routine

Follow [`examples/bot/ROUTINE_EVENING.md`](../examples/bot/ROUTINE_EVENING.md).
Summary:

1. `GET /health`. If HTTP fails, **401**, or `ok` is not `true`, one chat
   ping (`tva health not ok`), stop. Do not dump the body.
2. `GET /sessions/latest`.
   - **404** / no sessions → ping `tva missing (no session)`, stop. No log line.
   - If `session.id` does **not** start with today’s Europe/Vienna date
     (`YYYY-MM-DD_`) → ping `tva missing (no tape)`, stop. Do **not**
     write a note or a log line for yesterday.
   - If `insights` is non-null → go to step 4 (write the note).
   - If any of `ingest` / `transcribe` / `extract` is `failed` → ping
     `failed` + `status.error`, write a `status failed` log line for
     **this** id, stop (no note).
   - If `ingest` is not `ok` → ping `tva missing (ingest)`, write
     `status missing`, stop.
   - If `transcribe` or `extract` is `running` → do not POST; poll
     `/status` (same wait as step 3).
   - If `status.stages.extract == missing` and `ingest == ok` →
     `POST /sessions/{id}/run?stages=transcribe,extract`. **202** starts
     the job. **409** means a run is already in flight — poll, do not
     retry the POST.
3. Poll `GET /sessions/{id}/status` every few seconds until `transcribe`
   and `extract` are not `running`. Timeout ~15 minutes → ping
   `tva missing (run still running)`, write `status missing`, stop.
   Then `GET /sessions/{id}` (not “latest”, so you stay on this id).
   - If extract is `failed` → ping `failed` + `error`, log, stop.
   - If `insights` is still null → ping `missing`, log, stop.
4. **Write the bot’s own Notion session note** from `insights` (template
   in §6). German quotes verbatim; English commentary allowed. Verify
   every quote against the bundle’s `transcript` (find `segment.id ==
   span.seg`) before writing.
5. **Prepend one log line** to the *TVA runs* Notion page (Vienna clock):
   `YYYY-MM-DD HH:mm Vienna | session <id> | segments N | events N | status ok`
   Use `status failed` / `status missing` when that is the outcome
   instead of writing a note. The date on the left is “when written”;
   the watchdog keys off the **session id**, not that timestamp.
6. **Ping in chat only** when any stage is `failed` or `missing`, or when
   `gaps` block a note (empty insights after a successful extract still
   gets a short note listing the gaps — that is success, no ping).

---

## 5. 23:30 watchdog (Europe/Vienna)

Same bot, later the same calendar day.

1. `GET /health`. Not ok / 401 / HTTP fail → ping, stop. Do not dump `root`.
2. Read the *TVA runs* page. Stop quietly **only** if a line already
   names a session id that starts with **today’s** Vienna date **and**
   says `status ok`. A line dated today that names **yesterday’s**
   session does **not** count.
3. `GET /sessions?days=1` (today only). You may instead `GET
   /sessions/latest` and keep it only if the id starts with today.
   - If a session id starts with today’s date and extract is `ok` but
     there is still no `status ok` log line for that id → write the log
     line if a Notion note titled `TVA <id>` exists; otherwise ping
     `tva missing (no evening log)`.
   - If a session id starts with today’s date and extract is
     `missing`, `failed`, or `running` → ping that state.
   - If no session for today → ping `tva missing (no tape)`.
4. Do not invent a session. Do not call anything but the API. Do not
   write a success log line for a non-today id.

---

## 6. Notion session note (evening)

Title: `TVA <session.id>`.

Body, in this order (omit a section if the list is empty; do not invent):

1. **Status** — stages + `provider`/`model`/`prompt_version` from
   `transcript` and `insights` (not from `status`). Do not paste
   `recording.path`.
2. **Bias** — each `bias_statements[]` as `> quote` + `seg`.
3. **Playbooks** — `playbooks_mentioned[]` (`name` if present).
4. **Spoken levels** — `stated_levels[]` (`token` if present). Label
   “spoken, not lab truth”.
5. **Stops / targets** — `stated_stops_targets[]`. Keep `raw_text`.
   Do not parse into a number.
6. **Check-ins / tilt / brief refs / observations**.
7. **Events** — `session_events[]` as `t kind seg`.
8. **Summaries** — `summary_de` then `summary_en`, labelled. Drop a
   summary if any **digit run** (`/\d+/`, e.g. `18500`) is not an
   **exact** digit run in a **cited** segment (a segment referenced by a
   kept span or event). `1` inside `18500` does not justify `1`. Drop
   `summary_de` if it is longer than 120 words.
9. **Gaps** — `insights.gaps[]` as a bullet list.

Every `> quote` must be a substring of the segment whose `id` equals
`span.seg`. If verification fails, skip that span and add a line under
Gaps: `dropped uncited quote (seg_…)`.

---

## 7. Not yet (do not claim these exist)

| Output | Milestone | Absent |
|---|---|---|
| keyframes, ROI OCR, chapter clips, VLM notes | **TVA3** | no |
| fills.parquet, trades.parquet | **TVA4** | no |
| alignment, per-trade evidence, rule scorecard | **TVA5** | no |
| briefs / DRC join, lab attribution | **TVA6** | no (`tva context`; CLI only) |
| debrief | **TVA6** | no (`tva report`; CLI only) |
| ledger | **TVA6** | no (`tva ledger add` / `tva rollup`; `GET /ledger/summary`) |
| Notion Session Debrief publish | **TVA6** | no (`tva publish --notion`; off by default; bot may keep writing the note) |
| intent-tag proposals | **TVA7** | no (`tva proposals`; CLI only; not a bot run stage) |
| coach experiments | **TVA7** | yes |

Do not write “no trades today” from the tape. Do not grade the day. Do
not mention AMP/TopstepX fills. Those pages stay with the bots that
already own them.

---

## 8. Sunday audit

Follow [`examples/bot/ROUTINE_SUNDAY_AUDIT.md`](../examples/bot/ROUTINE_SUNDAY_AUDIT.md).

1. `GET /health` — not ok / 401 / HTTP fail → ping, stop.
2. `GET /sessions?days=7` — list ids (oldest first). Empty list → log
   `sunday-audit | sessions 0 | missing 0 | failed 0`, ping
   `tva missing (no sessions in 7 days)`, stop.
3. For each id, `GET /sessions/{id}/status` (or use `?status=missing,failed`
   to find problems first — then still walk the full `days=7` list so
   `ok` and `running` days appear).
4. Write **one** Sunday table on the *TVA runs* page: id, ingest,
   transcribe, extract, error. Show the live state
   (`ok`/`missing`/`failed`/`running`). No quotes required.
5. One log line:
   `YYYY-MM-DD HH:mm Vienna | sunday-audit | sessions N | missing M | failed F`
   `M` = any stage `missing`; `F` = any stage `failed`; also count
   `R` = any stage `running` (in-flight is not a clean week).
6. Ping **only** if `M + F + R > 0` (or N = 0), with the ids. Quiet if
   the week is all `ok`.
