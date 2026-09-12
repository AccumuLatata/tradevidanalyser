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

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
tva doctor
```

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
tva serve --host 127.0.0.1 --port 8764
```

OBS filenames must be `%CCYY-%MM-%DD %hh-%mm-%ss`. Record to the trading PC
disk, then copy the file into `$TVA_ROOT/recordings/` (or pass the path to
`tva ingest`, which copies it there).

ASR and extraction default to **fake** providers (`TVA_ASR_PROVIDER=fake`,
`TVA_EXTRACT_PROVIDER=fake`) so CI and first-run never call a paid API.
WhisperX and Grok adapters are stubs until TVA1/TVA2.

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
| [`docs/GLOSSARY.md`](docs/GLOSSARY.md) | German speech + English level tokens |
