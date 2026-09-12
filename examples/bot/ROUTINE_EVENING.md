# ROUTINE — evening (after the NY tape)

Run after the session is expected on the store. Europe/Vienna for clocks
and log lines.

## Steps

1. `GET /health`
   - If HTTP fails or `ok` is not `true`: ping chat `tva health not ok`,
     stop.

2. `GET /sessions/latest`
   - **404** or empty: ping `tva missing (no session)`, stop.
   - Read `session.id`, `status.stages`, `insights`, `transcript`.

3. If `status.stages.extract` is `failed` or `transcribe` is `failed`:
   ping `tva failed session <id> — {error}`, stop. No Notion note.

4. If `insights` is `null` and `ingest` is `ok`:
   - `POST /sessions/{id}/run?stages=transcribe,extract`
   - Expect **202**. Poll `GET /sessions/{id}/status` until `transcribe`
     and `extract` are not `running` (timeout ~15 minutes, then ping
     `tva missing (run still running)` and stop).
   - `GET /sessions/latest` again (or `GET /sessions/{id}`).
   - If extract is still `missing` or `failed`: ping that state, stop.

5. If `insights` is present:
   - Verify each span: `span.text` is a substring of the segment with
     `span.seg` in `transcript.segments`. Drop failures into a local
     gaps list.
   - Write **your** Notion session note titled `TVA <id>`:
     status, bias, playbooks, spoken levels (label “not lab truth”),
     stops/targets as `raw_text` (do not parse prices), check-ins, tilt,
     brief refs, observations, events, `summary_de` / `summary_en`
     (drop a summary if it has a digit not in any cited segment), gaps.
   - German quotes verbatim. English around them is fine.

6. Prepend **one** line to the *TVA runs* Notion page:

   ```
   YYYY-MM-DD HH:mm Vienna | session <id> | segments N | events N | status ok
   ```

   `N` from `transcript.segments` and `insights.session_events`.
   Use `status failed` or `status missing` instead of `ok` when you
   pinged and did not write a full note.

7. Ping chat **only** on `failed` or `missing`. No ping on success.

## Watchdog — 23:30 Europe/Vienna

1. `GET /health` — not ok → ping, stop.
2. If *TVA runs* already has a line for **today’s** Vienna date → stop.
3. `GET /sessions/latest` (or `?days=1`).
   - Session id starts with today and extract `ok` but no log line →
     ping `tva missing (no evening log)`.
   - Session id starts with today and extract `missing`/`failed` → ping.
   - No session for today → ping `tva missing (no tape)`.
4. Do not invent a session. Do not call non-API tools.
