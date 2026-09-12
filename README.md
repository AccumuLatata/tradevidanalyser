# TradeVidAnalyser

Headless, bot-first service that turns recorded futures trading sessions
(OBS screen + spoken commentary, **German**) into a time-stamped **Session
Record** and a small **HTTP API** the desk's Grok bots can query — without
anyone opening a video.

v1 answers: *what was said on the tape?* It does **not** join ThesisTester
or import fills. TradesViz (AMP + TopstepX) and the lab are a later phase.

Series code **TVA**. Locked decisions: [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md).

## Install

Python 3.11+, `ffmpeg` / `ffprobe` on PATH.

| Host | Install | `TVA_ASR_PROVIDER` | Notes |
|---|---|---|---|
| CI / first-run | `pip install -e ".[dev]"` | `fake` (default) | No GPU, no model download, no API key |
| Trading PC OCR | `pip install -e ".[dev,ocr]"` | ASR unchanged; `TVA_OCR_PROVIDER=paddleocr` | Optional PaddleOCR extra. Fake OCR stays the default |
| Trading PC (CUDA) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=float16`; install a CUDA torch wheel from pytorch.org if pip gave you CPU-only |
| Mac (CPU) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=int8` + a warning; slow. Prefer the PC GPU or hosted ASR |
| Mac hosted fallback | `pip install -e ".[dev]"` | `deepgram` | `DEEPGRAM_API_KEY`; uploads `audio/mic.opus` only. Scribe is reserved, not the PR-05 pick |

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # fake ASR (default)
# pip install -e ".[dev,whisperx]" # local WhisperX (PC CUDA or Mac CPU)
tva doctor
```

WhisperX and Deepgram are **opt-in**. Leave `TVA_ASR_PROVIDER` unset (or
`fake`) unless you have the extra / key. Optional knobs: `TVA_ASR_MODEL`
(WhisperX `large-v3`, Deepgram `nova-3`), `TVA_ASR_BATCH_SIZE`,
`TVA_ASR_DEVICE`, `TVA_ASR_COMPUTE_TYPE`, `DEEPGRAM_API_KEY`.

Set `TVA_ROOT` to the shared store (Synology mount, or a local folder):

```bash
export TVA_ROOT=/Volumes/tradevid   # Mac
# setx TVA_ROOT T:\tradevid         # Windows, then new terminal
```

If unset, the CLI uses `./.tva_store` under the current directory.

## Commands

```bash
tva ingest "2026-09-11 14-30-00.mp4"
tva ingest "2026-09-11 14-30-00.mp4" --desktop-track
tva transcribe 2026-09-11_143000
tva extract 2026-09-11_143000
tva run --latest
tva run "2026-09-11 14-30-00.mp4" --desktop-track
tva status
tva doctor
tva watch --source /path/to/obs --once
tva wer 2026-09-11_143000 --ref path/to/reference.txt
tva frames 2026-09-11_143000 --contact-sheet
tva ocr 2026-09-11_143000
tva clips 2026-09-11_143000 --redact
tva vlm 2026-09-11_143000
tva serve --host 127.0.0.1 --port 8764
```

OBS filenames must be `%CCYY-%MM-%DD %hh-%mm-%ss`. Record to the trading PC
disk, then `tva watch --source <obs dir>` copies into `$TVA_ROOT/recordings/`
(or pass the path to `tva ingest`). Watch never deletes the source and
never records onto the NAS. OBS auto-split siblings (same filename prefix,
next start within 5 s of the previous duration) stitch into one session.
`--desktop-track` writes `audio/desktop.opus` and is never transcribed by
default.

`tva wer` prints JSON `{wer, jargon_recall, n_words}` against a
hand-corrected reference (the golden excerpt on the NAS). It uses
[`docs/GLOSSARY.md`](docs/GLOSSARY.md) for jargon tokens.

`tva frames <id>` writes JPEGs at chapter markers ± 0/2/5 s (or `--at t…`)
to `$TVA_ROOT/sessions/<id>/frames/`. `--contact-sheet` adds
`frames/contact_sheet.jpg` so ROIs in [`layout.yaml`](layout.yaml) can be
measured. Raw frames stay under `TVA_ROOT` (PII).

`tva ocr <id>` reads those frames through `layout.yaml` ROIs and writes
`ocr.parquet` (`t, roi, text, confidence, parsed`). Clock parses to ISO
time, P&L to a signed float, position to an int. Fake OCR is the default
(`TVA_OCR_PROVIDER=fake`); `pip install 'tradevidanalyser[ocr]'` +
`TVA_OCR_PROVIDER=paddleocr` is opt-in and never required in CI.

`tva clips <id>` writes chapter windows (marker −30 s … +60 s) as
`clips/<t>.mp4` with the mic track only (`-c copy`). `--redact` applies
`layout.yaml` `*_mask` ROIs as black boxes (re-encodes video). The same
masks are burned into every JPEG `tva frames` writes.

`tva vlm <id>` is **off unless `TVA_VLM_PROVIDER` is set** (not even fake
by default). Input is redacted frames and/or clips — never the raw
recording. Grok sends JPEG `image_url` data URLs (`docs.x.ai` image
understanding; Imagine `video_url` is generation, not used). Gemini can
upload a redacted clip via the File API. Notes are stored on
`insights.visual_notes` (`frames_cited[]`) and `visual_notes.json`; a
note that states a number absent from `ocr.parquet` is dropped. Cost is
added to `status.json` `cost_usd`.

ASR and extraction default to **fake** providers (`TVA_ASR_PROVIDER=fake`,
`TVA_EXTRACT_PROVIDER=fake`) so CI and first-run never call a paid API.
WhisperX is the local adapter (`TVA_ASR_PROVIDER=whisperx`). Hosted fallback
is Deepgram (`TVA_ASR_PROVIDER=deepgram`, audio only). Grok extract is opt-in
(`TVA_EXTRACT_PROVIDER=grok`, `XAI_API_KEY`, optional `TVA_EXTRACT_MODEL`,
default `grok-4.6`). The German prompt lives in
[`prompts/insights_v1.de.md`](prompts/insights_v1.de.md).

## API (Grok)

```
GET /health
GET /sessions?days=7&status=missing,failed
GET /sessions/latest
GET /sessions/{id}
GET /sessions/{id}/status
GET /sessions/{id}/transcript
GET /sessions/{id}/insights
GET /sessions/{id}/clips/{name}     # 404 unless TVA_SERVE_MEDIA=1
POST /sessions/{id}/run?stages=transcribe,extract   # 202, poll /status
```

`GET /sessions?days=7` keeps session ids whose filename date is within the
last 7 calendar days. `status` is a comma-separated stage state
(`ok|missing|failed|running`); a session matches if any stage is in that set.

`POST /sessions/{id}/run` starts `transcribe` / `extract` on a background
thread and sets `status.stages[x]=running`. Poll `GET /sessions/{id}/status`.
Committed response shapes live in [`examples/api/`](examples/api/).

Bind `tva serve` to localhost or a Tailscale interface. Off-loopback binds
(including `0.0.0.0`) require `TVA_API_TOKEN`; clients send
`Authorization: Bearer <token>`. Without a token those binds are refused
and requests get 401. Do not port-forward the API to the public internet.

```bash
export TVA_API_TOKEN=…          # required off-loopback
tva serve --host 127.0.0.1 --port 8764
# curl -H "Authorization: Bearer $TVA_API_TOKEN" http://mac:8764/sessions/latest
```

## Tests

```bash
ruff check src tests
pytest -q
```

## Documents

| Doc | What it is |
|---|---|
| [`docs/01_DISCOVERY.md`](docs/01_DISCOVERY.md) | Desk as it is, where the gap is |
| [`docs/02_SCOPE.md`](docs/02_SCOPE.md) | Job, v1 vs later, principles |
| [`docs/03_STATE_OF_THE_ART.md`](docs/03_STATE_OF_THE_ART.md) | ASR / VLM research |
| [`docs/04_ARCHITECTURE.md`](docs/04_ARCHITECTURE.md) | NAS, API, Session Record |
| [`docs/05_ROADMAP.md`](docs/05_ROADMAP.md) | TVA0–TVA3 and locked D1–D9 |
| [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) | Build contract: PR-00…PR-27, contracts, acceptance |
| [`docs/TVA_GROK_ROUTINE_PACK.md`](docs/TVA_GROK_ROUTINE_PACK.md) | Grok bot pack: API only, evening, watchdog, Sunday |
| [`examples/bot/`](examples/bot/) | Copy-ready `SYSTEM.md` + evening + Sunday routines |
| [`docs/GOLDEN_EXCERPT.md`](docs/GOLDEN_EXCERPT.md) | Cut, redact, hand-correct the WER reference |
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | German speech + English level tokens |
