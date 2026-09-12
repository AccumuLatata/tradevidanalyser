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
