# TradeVidAnalyser — Grok Bot routine pack (stub, completed in PR-11)

**Status:** stub. Surfaces and hard rules are final; the evening routine is
filled in when PR-10 (API hardening) and PR-11 land.
**Plan:** [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) §5 PR-11.

TradeVidAnalyser does **not** embed Grok, host a queue, or ship an MCP
server. The bot talks HTTP to `tva serve` on the always-on Mac.

## Surfaces the bot may use

| Surface | When | Notes |
|---|---|---|
| `GET /health` | start of every routine | `ok: false` → ping, stop |
| `GET /sessions?days=7` | Sunday audit | list + stage states |
| `GET /sessions/latest` | evening routine | session + status + transcript + insights |
| `GET /sessions/{id}/insights` | on demand | cited spans only |
| `GET /sessions/{id}/transcript` | to verify a quote | the only source of quotes |
| `POST /sessions/{id}/run` | only if `status.stages.extract == missing` | idempotent |

The bot never mounts the NAS, never runs `ffmpeg`, never opens a video.

## Hard rules

| Forbidden | Why |
|---|---|
| Quote text that is not in `/transcript` | citations are the trust model |
| Turn a spoken number into a price, P&L or count | numbers come from fills/OCR (later); speech is `raw_text` |
| Store or print a LAN IP, the API token, or `TVA_ROOT` | secrets and topology stay out of Notion |
| Call anything but the API | CLI is for the scheduler on the LAN |
| Edit `insights.json` / `transcript.json` | facts are files the app owns |
| Report `unverifiable` as pass or fail | it means "no evidence", nothing more |

## Not yet (do not claim these exist)

| Output | Arrives in |
|---|---|
| fills, trades, alignment, rule scorecard | TVA4–TVA5 |
| brief-vs-behaviour, lab attribution, debrief page, ledger | TVA6 |
| intent-tag proposals, coach experiments | TVA7 |

## Evening routine (to be completed in PR-11)

1. `GET /health` → if not ok, one chat ping, stop.
2. `GET /sessions/latest` → if `insights` is null and a recording exists
   for today, `POST /sessions/{id}/run`, wait, re-read.
3. Write the bot's own Notion note from `insights` (German quotes
   verbatim; English commentary allowed).
4. Prepend one log line to the *TVA runs* Notion page:
   `YYYY-MM-DD HH:mm Vienna | session <id> | segments N | events N | status ok`.
5. Ping in chat **only** on `failed`, `missing`, or gaps that block a note.
6. 23:30 watchdog: no log line → ping.
