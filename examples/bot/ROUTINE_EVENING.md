# ROUTINE — evening (after the NY tape)

Run after the session is expected on the store. Europe/Vienna for clocks,
“today”, and log lines.

## Steps

1. `GET /health`
   - If HTTP fails, **401**, or `ok` is not `true`: ping chat
     `tva health not ok`, stop.
   - Use only `ok`. Never copy `root` (that is `TVA_ROOT`).

2. `GET /sessions/latest`
   - **404** or empty: ping `tva missing (no session)`, stop. No log line.
   - If `session.id` does **not** start with today’s Europe/Vienna date
     (`YYYY-MM-DD_`): ping `tva missing (no tape)`, stop. Do not write a
     note or a log line for a previous day (that would silence the
     23:30 watchdog).
   - Read `session.id`, `status.stages`, `status.error`, `insights`,
     `transcript`.

3. If any of `ingest` / `transcribe` / `extract` is `failed`:
   ping `tva failed session <id> — {error}`, prepend a *TVA runs* line
   with `status failed` for this id, stop. No Notion note.

4. If `ingest` is not `ok`:
   ping `tva missing (ingest)`, log `status missing`, stop.

5. If `insights` is `null` and `ingest` is `ok`:
   - If `transcribe` or `extract` is already `running`: do **not** POST.
     Poll `GET /sessions/{id}/status`.
   - Else `POST /sessions/{id}/run?stages=transcribe,extract`.
     - **202**: job accepted (`accepted`, `session_id`, `stages`, `status`).
     - **409**: run already in progress — poll, do not POST again.
     - **404**: session vanished — ping `missing`, stop.
   - Poll `GET /sessions/{id}/status` until `transcribe` and `extract`
     are not `running` (timeout ~15 minutes, then ping
     `tva missing (run still running)`, log `status missing`, stop).
   - `GET /sessions/{id}` (stay on this id; do not switch to a newer
     `latest`).
   - If extract is `failed`: ping `tva failed session <id> — {error}`,
     log `status failed`, stop.
   - If extract is still `missing` or `insights` is still `null`: ping
     that state, log `status missing`, stop.

6. If `insights` is present (object; lists may be empty):
   - Verify each span and event: find the segment whose `id` equals
     `span.seg` in the `transcript.segments` **array**. `span.text` must
     be a substring of that segment’s `text`. Drop failures into a local
     gaps list. Do not treat `segments` as a dict.
   - Write **your** Notion session note titled `TVA <id>`:
     status (stages + transcript/insights `provider`/`model`/
     `prompt_version` — not `recording.path`), bias, playbooks, spoken
     levels (label “not lab truth”), stops/targets as `raw_text` (do not
     parse prices), check-ins, tilt, brief refs, observations, events,
     `summary_de` / `summary_en` (drop a summary if a digit run is not an
     exact digit run in a cited segment, or if `summary_de` is over 120
     words), gaps.
   - German quotes verbatim. English around them is fine.

7. Prepend **one** line to the *TVA runs* Notion page:

   ```
   YYYY-MM-DD HH:mm Vienna | session <id> | segments N | events N | status ok
   ```

   `N` from `transcript.segments` and `insights.session_events`.
   Use `status failed` or `status missing` instead of `ok` when you
   pinged and did not write a full note. The watchdog keys off the
   **session id**, not the write-timestamp on the left.

8. Ping chat **only** on `failed` or `missing`. No ping on success.

## Watchdog — 23:30 Europe/Vienna

1. `GET /health` — not ok / 401 / HTTP fail → ping, stop. Do not dump `root`.
2. Stop quietly **only** if *TVA runs* already has a line whose session
   id starts with **today’s** Vienna date and says `status ok`. A
   today-dated line for yesterday’s session does not count.
3. `GET /sessions?days=1` (today only). Or `GET /sessions/latest` and
   keep it only if the id starts with today.
   - Session id starts with today and extract `ok` but no `status ok`
     line for that id → write the log line if a `TVA <id>` note exists;
     otherwise ping `tva missing (no evening log)`.
   - Session id starts with today and extract `missing` / `failed` /
     `running` → ping that state.
   - No session for today → ping `tva missing (no tape)`.
4. Do not invent a session. Do not call non-API tools. Do not write a
   `status ok` line for a non-today id.
