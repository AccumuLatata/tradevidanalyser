# ASR jargon glossary (German speech, English level tokens)

## How this is used

`glossary.py` parses the sections below (not this header) into:

- `initial_prompt` — tokens joined by spaces, capped at 220, for WhisperX
  and hosted keyword boost
- `level_tokens` — **Levels / locations** (ONH, dVWAP, …)
- `playbook_terms` — **Playbooks / rules** (3c, Scalp, …)

`FakeExtractProvider` and later ASR adapters read these lists from this
file. They are not hard-coded in Python. Keep the lists short — long
prompts dilute the model.

WER / jargon-recall (`tva wer`) scores English tokens inside German
speech against this same list.

## Levels / locations

ONH, ONL, pdH, pdL, pdPOC, pdVAH, pdVAL, pdEQ, pwEQ, pwVAH, pwVAL, pmVAH,
dVWAP, wVWAP, mVWAP, p30VWAP, p30POC, APOC, HVL, VA, IB, NY open, ETH

## Playbooks / rules (spoken)

Playbook, Skalp, Scalp, Swing, Touch, 3c, counter, CTR, Arrival, Check-in,
Stundencheck, Bias, Long, Short, Stop, Ziel, Target, Invalidation, Tilt,
No-trade, Trade zone, Re-entry, MFR, ONH Touch

## Platform

Quantower, Topstep, TopstepX, AMP, TradesViz, MenthorQ, Grok, Brief
