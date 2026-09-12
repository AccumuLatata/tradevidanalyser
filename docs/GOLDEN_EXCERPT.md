# Golden excerpt — recipe

The WER and extraction gates in the plan run against **one redacted real
excerpt** that lives on the NAS, never in git.

## Record (or cut from a real session)

- 20–30 minutes of a real NY session, German commentary.
- Must contain, spoken: a bias ("Bias ist long…"), a named playbook
  ("ONH Touch Scalp"), one stop and one target ("Stop unter … Ziel …"),
  one hourly check-in, one reference to the brief or to Grok.
- Optional: one chapter marker (hotkey) at a trade.

## Cut and redact with ffmpeg

```bash
# cut 20 minutes starting at 01:10:00, stream copy
ffmpeg -ss 01:10:00 -to 01:30:00 -i "2026-09-11 14-30-00.mp4" -c copy excerpt_raw.mp4

# black-box the account / balance regions (coordinates from layout.yaml, pixels)
ffmpeg -i excerpt_raw.mp4 \
  -vf "drawbox=x=1500:y=980:w=420:h=100:color=black:t=fill,drawbox=x=0:y=0:w=300:h=40:color=black:t=fill" \
  -c:a copy "2026-09-11 15-40-00.mp4"
```

Keep the OBS filename shape so `tva ingest` accepts it. The `15-40-00`
should be the real wall-clock start of the cut so alignment tests are
meaningful.

## Hand-correct the transcript once

1. `tva ingest` + `tva transcribe --provider whisperx` (or hosted).
2. Copy `transcript.json` → `reference.txt` (plain text, one segment per
   line) and fix every word. Keep English tokens as spoken (ONH, dVWAP).
3. Note the planted facts and their approximate video times in
   `planted.yaml` (`bias`, `playbook`, `stop_raw`, `target_raw`, `checkin_t`).

## Place it

```
TVA_ROOT/fixtures/golden/
  2026-09-11 15-40-00.mp4
  reference.txt
  planted.yaml
```

Tests marked `@pytest.mark.golden` look for this folder and skip if absent.
