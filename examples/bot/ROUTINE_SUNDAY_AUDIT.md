# ROUTINE — Sunday audit

Question-bot style. No quotes required. One table, one log line.

## Steps

1. `GET /health`
   - If HTTP fails or `ok` is not `true`: ping `tva health not ok`, stop.

2. `GET /sessions?days=7`
   - Body: `{ "sessions": ["YYYY-MM-DD_HHMMSS", …], "latest": "…" }`.
   - Empty list: log `sunday-audit | sessions 0 | missing 0 | failed 0`,
     no ping, stop.

3. Optional shortcut: `GET /sessions?days=7&status=missing,failed`
   to collect problem ids. Still walk the full `days=7` list for the
   table so `ok` days appear.

4. For each id in `sessions` (oldest first):
   `GET /sessions/{id}/status`
   Record `stages.ingest`, `stages.transcribe`, `stages.extract`, `error`.

5. Write **one** Sunday block on *TVA runs* (do not create a session
   note per id):

   | id | ingest | transcribe | extract | error |
   |---|---|---|---|---|
   | … | ok/missing/failed | … | … | … |

   Do not quote the tape. Do not mention fills, rules, briefs, or clips.
   Those outputs do not exist yet (TVA3–TVA7).

6. Count `M` = sessions with any stage `missing`, `F` = any stage
   `failed`. Prepend one log line (Vienna):

   ```
   YYYY-MM-DD HH:mm Vienna | sunday-audit | sessions N | missing M | failed F
   ```

7. Ping chat **only** if `M + F > 0`, listing those ids and states.
   Quiet if the week is all `ok`.

## Hard rules (same as system)

Never invent a quote. Never call anything but the API. Never store LAN
IPs or the token. Never treat speech as a fill.
