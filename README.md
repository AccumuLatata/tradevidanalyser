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
| Trading PC (CUDA) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=float16`; install a CUDA torch wheel from pytorch.org if pip gave you CPU-only |
| Mac (CPU) | `pip install -e ".[dev,whisperx]"` | `whisperx` | `compute_type=int8` + a warning; slow. Prefer the PC GPU or hosted ASR |
| Mac hosted fallback | extra lands in PR-05 | `deepgram` / `scribe` | Audio only; not in this PR |

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"            # fake ASR (default)
# pip install -e ".[dev,whisperx]" # local WhisperX (PC CUDA or Mac CPU)
tva doctor
```

WhisperX is **opt-in**. Leave `TVA_ASR_PROVIDER` unset (or `fake`) unless you
have the extra installed. Optional knobs: `TVA_ASR_MODEL` (default
`large-v3`), `TVA_ASR_BATCH_SIZE`, `TVA_ASR_DEVICE`, `TVA_ASR_COMPUTE_TYPE`.

Set `TVA_ROOT` to the shared store (Synology mount, or a local folder):

```bash
export TVA_ROOT=/Volumes/tradevid   # Mac
# setx TVA_ROOT T:\tradevid         # Windows, then new terminal
```

If unset, the CLI uses `./.tva_store` under the current directory.

## Commands

```bash
tva ingest "2026-09-11 14-30-00.mp4"
tva transcribe 2026-09-11_143000
tva extract 2026-09-11_143000
tva run --latest
tva run "2026-09-11 14-30-00.mp4"
tva status
tva doctor
tva wer 2026-09-11_143000 --ref path/to/reference.txt
tva serve --host 127.0.0.1 --port 8764
```

OBS filenames must be `%CCYY-%MM-%DD %hh-%mm-%ss`. Record to the trading PC
disk, then copy the file into `$TVA_ROOT/recordings/` (or pass the path to
`tva ingest`, which copies it there).

`tva wer` prints JSON `{wer, jargon_recall, n_words}` against a
hand-corrected reference (the golden excerpt on the NAS). It uses
[`docs/GLOSSARY.md`](docs/GLOSSARY.md) for jargon tokens.

ASR and extraction default to **fake** providers (`TVA_ASR_PROVIDER=fake`,
`TVA_EXTRACT_PROVIDER=fake`) so CI and first-run never call a paid API.
WhisperX is the local adapter (`TVA_ASR_PROVIDER=whisperx`); Grok extract
stays a stub until TVA2. Hosted ASR is PR-05.

## API (Grok)

```
GET /health
GET /sessions
GET /sessions/latest
GET /sessions/{id}
GET /sessions/{id}/transcript
GET /sessions/{id}/insights
POST /sessions/{id}/run
```

Bind `tva serve` to localhost or a Tailscale interface. Do not port-forward
it to the public internet.

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
| [`docs/TVA_GROK_ROUTINE_PACK.md`](docs/TVA_GROK_ROUTINE_PACK.md) | What the bot may call, hard rules |
| [`docs/GOLDEN_EXCERPT.md`](docs/GOLDEN_EXCERPT.md) | Cut, redact, hand-correct the WER reference |
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | German speech + English level tokens |
