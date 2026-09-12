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
Base: `$TVA_API_BASE` (no trailing slash).

| Surface | When | Notes |
|---|---|---|
| `GET /health` | start of every routine | `ok: false` → one chat ping, stop |
| `GET /sessions?days=7` | Sunday audit | `{sessions: [id…], latest}` |
| `GET /sessions?days=7&status=missing,failed` | Sunday / watchdog | sessions that need a ping |
| `GET /sessions/latest` | evening | bundle: `session`, `status`, `transcript`, `insights` |
| `GET /sessions/{id}` | same bundle for a known id | 404 if unknown |
| `GET /sessions/{id}/status` | after `POST …/run` | poll until no stage is `running` |
| `GET /sessions/{id}/insights` | on demand | cited spans only |
| `GET /sessions/{id}/transcript` | to verify a quote | **only** source of quotes |
| `POST /sessions/{id}/run?stages=transcribe,extract` | only if extract is `missing` and ingest is `ok` | **202**; poll `/status` |

**Do not call:** CLI (`tva …`), NAS paths, `ffmpeg`, video files,
`GET /sessions/{id}/clips/…` (gated; TVA3). Do not `POST` any other path.

The bot never mounts the NAS, never runs `ffmpeg`, never opens a video.

---

## 2. What the JSON means

`GET /sessions/latest` (see `examples/api/latest.json`):

| Key | Meaning |
|---|---|
| `session.id` | `YYYY-MM-DD_HHMMSS` (Vienna clock from the OBS filename) |
| `session.recording` | path, hash, duration, chapters, optional `parts[]` |
| `status.stages` | `ingest` / `transcribe` / `extract` → `ok\|missing\|failed\|running` |
| `status.error` | last stage error or `null` |
| `transcript.segments[]` | `{id, t0, t1, lang, text, words}` — `id` is `seg_NNN` |
| `insights` | cited extraction, or `null` if extract has not run |

Insights fields (each span is `{seg, text, t?, name?, token?, raw_text?}`
except `gaps` which are strings):

`bias_statements`, `playbooks_mentioned`, `stated_levels`,
`stated_stops_targets`, `checkins`, `tilt_markers`, `brief_refs`,
`observations`, `gaps`, `session_events[{t, kind, seg, text}]`,
`summary_de`, `summary_en`.

`kind` is a closed set: `hourly_checkin`, `bias_statement`,
`no_trade_zone`, `trade_zone`, `tilt`, `break`, `rule_mention`,
`brief_ref`, `grok_ref`.

`text` on a span or event **must** be a substring of
`transcript.segments[seg].text`. If it is not, drop it — do not “fix” it.

---

## 3. Hard rules

| Forbidden | Why |
|---|---|
| Invent a quote, or paraphrase a quote as if it were verbatim | citations are the trust model |
| Quote text that is not a substring of its `/transcript` segment | every quote must exist in `/transcript` |
| Treat a spoken number / price / “Ziel 18k” as a fill, P&L, or count | numbers come from fills/OCR later; speech stays `raw_text` |
| Call anything but this API | CLI is for the scheduler on the LAN |
| Store or print a LAN IP, the API token, or `TVA_ROOT` | secrets and topology stay out of Notion and chat |
| Put `TVA_API_TOKEN` in a page, log line, or screenshot | token is a secret |
| Edit `insights.json` / `transcript.json` (or ask the API to) | facts are files the app owns |
| Report `unverifiable` as pass or fail | it means “no evidence”, nothing more |
| Claim TVA3–TVA7 outputs exist | see §7 |

Quiet on success: if stages are `ok` and a note was written, **do not**
ping chat. One log line on *TVA runs* is enough.

---

## 4. Evening routine

Follow [`examples/bot/ROUTINE_EVENING.md`](../examples/bot/ROUTINE_EVENING.md).
Summary:

1. `GET /health`. If `ok` is not `true`, one chat ping (`tva health not ok`), stop.
2. `GET /sessions/latest`.
   - **404** / no sessions → ping `missing` (no tape), stop.
   - If `insights` is non-null → go to step 4.
   - If `status.stages.extract == missing` and `ingest == ok` →
     `POST /sessions/{id}/run?stages=transcribe,extract` (202). Poll
     `GET /sessions/{id}/status` every few seconds until `transcribe` and
     `extract` are not `running`. Then `GET /sessions/latest` again.
   - If any stage is `failed` → ping `failed` + `status.error`, stop (no note).
3. If after the wait `insights` is still null → ping `missing`, stop.
4. **Write the bot’s own Notion session note** from `insights` (template
   in §6). German quotes verbatim; English commentary allowed. Verify
   every quote against `/transcript` (or the bundle’s `transcript`)
   before writing.
5. **Prepend one log line** to the *TVA runs* Notion page (Vienna clock):
   `YYYY-MM-DD HH:mm Vienna | session <id> | segments N | events N | status ok`
   Use `status failed` / `status missing` when that is the outcome
   instead of writing a note.
6. **Ping in chat only** when any stage is `failed` or `missing`, or when
   `gaps` block a note (empty insights after a successful extract still
   gets a short note listing the gaps — that is success, no ping).

---

## 5. 23:30 watchdog (Europe/Vienna)

Same bot, later the same calendar day.

1. `GET /health`. Not ok → ping, stop.
2. Read the *TVA runs* page. If a log line for **today’s** Vienna date
   already exists → stop, no ping.
3. `GET /sessions?days=1` (or `/sessions/latest`).
   - If a session id starts with today’s date and extract is `ok` but
     there is still no log line → write the log line if a note exists;
     otherwise ping `missing` (note not written).
   - If a session id starts with today’s date and extract is
     `missing` or `failed` → ping that state.
   - If no session for today → ping `missing` (no tape / no ingest).
4. Do not invent a session. Do not call anything but the API.

---

## 6. Notion session note (evening)

Title: `TVA <session.id>`.

Body, in this order (omit a section if the list is empty; do not invent):

1. **Status** — stages + provider/model/`prompt_version`.
2. **Bias** — each `bias_statements[]` as `> quote` + `seg`.
3. **Playbooks** — `playbooks_mentioned[]` (`name` if present).
4. **Spoken levels** — `stated_levels[]` (`token` if present). Label
   “spoken, not lab truth”.
5. **Stops / targets** — `stated_stops_targets[]`. Keep `raw_text`.
   Do not parse into a number.
6. **Check-ins / tilt / brief refs / observations**.
7. **Events** — `session_events[]` as `t kind seg`.
8. **Summaries** — `summary_de` then `summary_en`, labelled. Drop a
   summary if it contains a digit that does not appear in any **cited**
   segment text.
9. **Gaps** — `insights.gaps[]` as a bullet list.

Every `> quote` must be a substring of `transcript.segments[seg].text`.
If verification fails, skip that span and add a line under Gaps:
`dropped uncited quote (seg_…)`.

---

## 7. Not yet (do not claim these exist)

| Output | Milestone | Absent |
|---|---|---|
| keyframes, ROI OCR, chapter clips, VLM notes | **TVA3** | yes |
| fills.parquet, trades.parquet | **TVA4** | yes |
| alignment, per-trade evidence, rule scorecard | **TVA5** | yes |
| briefs / DRC join, lab attribution, debrief, ledger | **TVA6** | yes |
| intent-tag proposals, coach experiments | **TVA7** | yes |

Do not write “no trades today” from the tape. Do not grade the day. Do
not mention AMP/TopstepX fills. Those pages stay with the bots that
already own them.

---

## 8. Sunday audit

Follow [`examples/bot/ROUTINE_SUNDAY_AUDIT.md`](../examples/bot/ROUTINE_SUNDAY_AUDIT.md).

1. `GET /health` — not ok → ping, stop.
2. `GET /sessions?days=7` — list ids.
3. For each id, `GET /sessions/{id}/status` (or use `?status=missing,failed`
   to find problems first).
4. Write **one** Sunday table on the *TVA runs* page: id, ingest,
   transcribe, extract, error. No quotes required.
5. One log line:
   `YYYY-MM-DD HH:mm Vienna | sunday-audit | sessions N | missing M | failed F`
6. Ping **only** if M+F > 0, with the ids. Quiet if the week is all `ok`.
