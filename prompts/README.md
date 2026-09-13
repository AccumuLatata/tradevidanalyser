# Extract prompts

Versioned system prompts for model-derived Session Record files.
`prompt_version` on `insights.json` is `{stem}+{sha256[:12]}` of the
prompt file bytes.

## `insights_v1.de.md`

German system prompt for `GrokExtractProvider` (PR-08).

- Explains each `Insights` field (`bias_statements`, `playbooks_mentioned`,
  `stated_levels`, `stated_stops_targets`, `checkins`, `tilt_markers`,
  `brief_refs`, `observations`, `gaps`).
- Demands a real `seg` id and a verbatim substring for every span.
- Forbids invented numbers; spoken figures stay in `raw_text`.

The client windows the transcript (≤ 40 segments, overlap 5), sends this
file as the system message, and constrains the reply to the Pydantic
`Insights` JSON Schema via xAI `response_format.json_schema`.

Do not edit in place to “improve” an old run: add a new file
(`insights_v2.de.md`) only after a plan amendment. Default model is
`grok-4.6` (`TVA_EXTRACT_MODEL`). Fake extract ignores this file.

## `events_v1.de.md`

Second-pass prompt for PR-09 (`session_events`, `summary_de`, `summary_en`).

- One call over the **whole** timeline with segment ids, times, and
  first-pass citation ids only (no transcript text). The client
  re-hydrates `SessionEvent.text` from the cited segment.
- Closed `kind` set: `hourly_checkin`, `bias_statement`, `no_trade_zone`,
  `trade_zone`, `tilt`, `break`, `rule_mention`, `brief_ref`, `grok_ref`.
- Summaries are labelled model prose. The citation guard drops a summary
  that contains a digit run not present in any cited segment, or a
  `summary_de` longer than 120 words.

`prompt_version` becomes `{insights_stem}+{hash}+{events_stem}+{hash}`.

## `stated_v1.de.md`

Per-trade window prompt for PR-19 (`tva evidence`). Used by
`GrokExtractProvider.stated_fields`. Fake extract ignores this file.

- Input is only the transcript segments that overlap the trade window.
- Fields: `setup`, `bias`, `stop_raw`, `target_raw`, `playbook`, each
  `{value, seg} | null`, plus `gaps`.
- Same citation rule as insights: verbatim substring, real `seg` id.

`prompt_version` on `evidence.json` is `{stem}+{sha256[:12]}` (Grok) or
`stated-keyword-v1` (fake).

## `vlm_v1.md`

System prompt for opt-in VLM notes (PR-15). Used by `GrokVlmProvider`
and `GeminiVlmProvider`. Fake ignores this file.

- Qualitative layout / structure only.
- `frames_cited` must be stems of attached JPEGs.
- Forbids numerals unless they appear in the OCR list in the user
  message. Empty OCR → no digits in `text`.
- Never the raw recording. Grok gets redacted JPEGs as `image_url`;
  Gemini may receive a redacted clip via File API.

`prompt_version` is `{stem}+{sha256[:12]}`. Off unless `TVA_VLM_PROVIDER`
is set. Default Grok model `grok-4.6` (`TVA_VLM_MODEL`).

## `debrief_v1.md`

English system prompt for `GrokReportProvider` (`tva report`, PR-23).
Fake report ignores this file.

- Prose only: day paragraph, brief-vs-behaviour, observations, three
  candidate learnings. The client renders source / trades / rules / gaps
  from files.
- Every span must cite an id from the user-supplied allow-list.
- Every digit run in prose must already appear in `trades.parquet`,
  `ocr.parquet`, or `context.json`.

`prompt_version` on `debrief.json` is `{stem}+{sha256[:12]}` (Grok) or
`debrief-fake-v1` (fake). Default model `grok-4.6` (`TVA_REPORT_MODEL`).
Fake is the default (`TVA_REPORT_PROVIDER=fake`).
