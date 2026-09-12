# ROUTINE — Sunday audit

Question-bot style. No quotes required. One table, one log line.

## Steps

1. `GET /health`
   - If HTTP fails, **401**, or `ok` is not `true`: ping `tva health not ok`,
     stop. Do not copy `root` (that is `TVA_ROOT`).

2. `GET /sessions?days=7`
   - Body: `{ "sessions": ["YYYY-MM-DD_HHMMSS", …], "latest": "…" }`.
   - `days=7` is today plus the previous six Vienna calendar days
     (`0 ≤ age < 7`). Ids are oldest-first. `latest` is the newest id
     **in this list**, not a global latest.
   - Empty list: log
     `YYYY-MM-DD HH:mm Vienna | sunday-audit | sessions 0 | missing 0 | failed 0`,
     ping `tva missing (no sessions in 7 days)`, stop.

3. Optional shortcut: `GET /sessions?days=7&status=missing,failed`
   to collect problem ids. A session matches if **any** stage is in that
   set. In-flight `running` does **not** match `missing` — still walk
   the full `days=7` list for the table so `ok` and `running` days
   appear.

4. For each id in `sessions` (oldest first, as returned):
   `GET /sessions/{id}/status`
   Record `stages.ingest`, `stages.transcribe`, `stages.extract`, `error`.
   Live values are `ok` / `missing` / `failed` / `running`.

5. Write **one** Sunday block on *TVA runs* (do not create a session
   note per id):

   | id | ingest | transcribe | extract | error |
   |---|---|---|---|---|
   | … | ok/missing/failed/running | … | … | … |

   Do not quote the tape. Do not mention fills, rules, briefs, or clips.
   Those outputs do not exist yet (TVA3–TVA7).

6. Count `M` = sessions with any stage `missing`, `F` = any stage
   `failed`, `R` = any stage `running`. Prepend one log line (Vienna):

   ```
   YYYY-MM-DD HH:mm Vienna | sunday-audit | sessions N | missing M | failed F
   ```

7. Ping chat **only** if `M + F + R > 0` (or N = 0), listing those ids
   and states. Quiet if the week is all `ok`.

## Hard rules (same as system)

Never invent a quote. Never call anything but the API. Never store LAN
IPs, `TVA_ROOT`, or the token. Never copy `GET /health` → `root`. Never
treat speech as a fill.
