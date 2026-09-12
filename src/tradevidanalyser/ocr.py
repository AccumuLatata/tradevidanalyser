"""ROI OCR. Fake is the default; PaddleOCR is an extra. Crops stay under TVA_ROOT."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

from tradevidanalyser import media, store
from tradevidanalyser.frames import Roi, get_layout
from tradevidanalyser.media import MediaError
from tradevidanalyser.schema import SessionRecord

SCHEMA_VERSION = "1"
OCR_ROIS = ("clock", "position", "pnl", "instrument")
MIN_PARSE_CONFIDENCE = 0.50
ENV_OCR_PROVIDER = "TVA_OCR_PROVIDER"
DEFAULT_PROVIDER = "fake"

_CLOCK = re.compile(
    r"(?P<h>\d{1,2})[:.\-](?P<m>\d{2})[:.\-](?P<s>\d{2})(?:[.,](?P<frac>\d{1,6}))?"
)
_INT = re.compile(r"(?P<sign>[+\-−–])?(?P<digits>\d+)")
_PNL = re.compile(
    r"(?P<sign>[+\-−–])?\s*(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+[.,]\d+|\d+)"
)


class OcrError(RuntimeError):
    pass


@dataclass(frozen=True)
class OcrRead:
    text: str
    confidence: float


@dataclass(frozen=True)
class OcrRow:
    t: float
    roi: str
    text: str
    confidence: float
    parsed: str | None


class OcrProvider(Protocol):
    name: str
    model: str

    def read(self, image: Path, *, roi: str) -> OcrRead: ...


class FakeOcrProvider:
    """Deterministic placeholder. CI never imports PaddleOCR.

    Reads, in order: constructor map ``{frame_stem:roi -> OcrRead}``, then a
    sidecar ``<frame_stem>.ocr.json`` next to the crop or the source frame.
    """

    name = "fake"
    model = "fake-v1"

    def __init__(self, reads: dict[str, OcrRead] | None = None) -> None:
        self._reads = dict(reads or {})

    def read(self, image: Path, *, roi: str) -> OcrRead:
        frame_stem = _frame_stem(image)
        for key in (f"{frame_stem}:{roi}", roi, image.stem):
            hit = self._reads.get(key)
            if hit is not None:
                return hit
        payload = _load_sidecar(image, frame_stem)
        if roi in payload:
            raw = payload[roi]
            return OcrRead(text=str(raw.get("text") or ""), confidence=float(raw.get("confidence") or 0.0))
        return OcrRead(text="", confidence=0.0)


class PaddleOcrProvider:
    """Local PaddleOCR (``pip install 'tradevidanalyser[ocr]'``). Never the default."""

    name = "paddleocr"
    model = "ppocr"

    def __init__(self) -> None:
        if not paddleocr_importable():
            raise OcrError("paddleocr is not installed; pip install 'tradevidanalyser[ocr]'")
        from paddleocr import PaddleOCR  # type: ignore[import-untyped]

        try:
            self._engine = PaddleOCR(lang="en", use_angle_cls=True, show_log=False)
        except TypeError:
            self._engine = PaddleOCR(lang="en")
        self.model = f"ppocr-{_paddleocr_version()}"

    def read(self, image: Path, *, roi: str) -> OcrRead:
        del roi
        texts, confs = _paddle_lines(self._engine, image)
        if not texts:
            return OcrRead(text="", confidence=0.0)
        confidence = sum(confs) / len(confs) if confs else 0.0
        return OcrRead(text=" ".join(texts).strip(), confidence=float(confidence))


def paddleocr_importable() -> bool:
    try:
        import paddleocr  # noqa: F401
    except Exception:
        return False
    return True


def get_ocr_provider(name: str | None = None) -> OcrProvider:
    chosen = (name or os.environ.get(ENV_OCR_PROVIDER) or DEFAULT_PROVIDER).strip().lower()
    chosen = chosen or DEFAULT_PROVIDER
    if chosen in {"fake", "test"}:
        return FakeOcrProvider()
    if chosen in {"paddleocr", "paddle", "ppocr"}:
        return PaddleOcrProvider()
    raise OcrError(f"unknown OCR provider {chosen!r}")


def parse_clock(text: str, *, prior: datetime) -> str | None:
    match = _CLOCK.search(text.replace(" ", ""))
    if not match:
        return None
    hour, minute, second = int(match["h"]), int(match["m"]), int(match["s"])
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        return None
    frac = match["frac"] or ""
    micro = int(frac.ljust(6, "0")[:6]) if frac else 0
    parsed = datetime(
        prior.year,
        prior.month,
        prior.day,
        hour,
        minute,
        second,
        micro,
        tzinfo=prior.tzinfo,
    )
    delta = (parsed - prior).total_seconds()
    if delta < -12 * 3600:
        parsed = parsed + timedelta(days=1)
    elif delta > 20 * 3600:
        parsed = parsed - timedelta(days=1)
    return parsed.isoformat()


def parse_pnl(text: str) -> str | None:
    match = _PNL.search(text.replace(" ", "").replace("'", ""))
    if not match:
        return None
    number = _parse_decimal(match["num"])
    if number is None:
        return None
    sign = match["sign"]
    if sign in {"-", "−", "–"}:
        number = -abs(number)
    return _format_float(number)


def parse_position(text: str) -> str | None:
    match = _INT.search(text.replace(" ", ""))
    if not match:
        return None
    digits = match["digits"]
    value = int(digits)
    if match["sign"] in {"-", "−", "–"}:
        value = -value
    return str(value)


def parse_roi(roi: str, text: str, *, prior: datetime, confidence: float) -> str | None:
    if confidence < MIN_PARSE_CONFIDENCE:
        return None
    if roi == "clock":
        return parse_clock(text, prior=prior)
    if roi == "pnl":
        return parse_pnl(text)
    if roi == "position":
        return parse_position(text)
    return None


def crop_roi(src: Path, roi: Roi, dest: Path) -> Path:
    """Crop ``src`` to ``roi`` (normalised 0..1) via ffmpeg. Dest stays local."""
    if roi.w <= 0 or roi.h <= 0:
        raise ValueError("empty roi")
    binary = media.which("ffmpeg")
    if not binary:
        raise MediaError("ffmpeg not on PATH")
    width, height = _image_size(src)
    x = max(int(roi.x * width), 0)
    y = max(int(roi.y * height), 0)
    w = max(int(roi.w * width), 1)
    h = max(int(roi.h * height), 1)
    if x + w > width:
        w = max(width - x, 1)
    if y + h > height:
        h = max(height - y, 1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(src),
            "-vf",
            f"crop={w}:{h}:{x}:{y}",
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(dest),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not dest.is_file() or dest.stat().st_size == 0:
        raise MediaError(result.stderr.strip() or f"ffmpeg crop failed for {src.name}")
    return dest


def ocr_frames(
    session: SessionRecord,
    *,
    root: Path,
    provider: OcrProvider | None = None,
    layout_id: str | None = None,
) -> list[OcrRow]:
    frames = store.list_frame_jpgs(root, session.id)
    if not frames:
        raise OcrError(f"no frames for {session.id}; run tva frames first")
    layout = get_layout(layout_id, root=root)
    engine = provider or get_ocr_provider()
    prior = datetime.fromisoformat(session.recording.start_wallclock_vienna)
    rows: list[OcrRow] = []
    with tempfile.TemporaryDirectory(prefix="tva-ocr-") as tmp:
        tmp_dir = Path(tmp)
        for frame in frames:
            t = float(frame.stem)
            sidecar = frame.with_name(f"{frame.stem}.ocr.json")
            if sidecar.is_file():
                target = tmp_dir / sidecar.name
                if not target.exists():
                    target.write_bytes(sidecar.read_bytes())
            for name in OCR_ROIS:
                roi = layout.rois.get(name)
                if roi is None or roi.w <= 0 or roi.h <= 0:
                    continue
                crop = tmp_dir / f"{frame.stem}_{name}.jpg"
                crop_roi(frame, roi, crop)
                read = engine.read(crop, roi=name)
                rows.append(
                    OcrRow(
                        t=t,
                        roi=name,
                        text=read.text,
                        confidence=float(read.confidence),
                        parsed=parse_roi(
                            name,
                            read.text,
                            prior=prior,
                            confidence=float(read.confidence),
                        ),
                    )
                )
    rows.sort(key=lambda row: (row.t, row.roi))
    return rows


def write_ocr_parquet(path: Path, rows: list[OcrRow]) -> Path:
    table = _rows_to_table(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    import pyarrow.parquet as pq

    pq.write_table(table, path, compression="none", coerce_timestamps=None)
    return path


def read_ocr_parquet(path: Path) -> list[OcrRow]:
    import pyarrow.parquet as pq

    table = pq.read_table(path)
    rows: list[OcrRow] = []
    for t, roi, text, confidence, parsed in zip(
        table.column("t").to_pylist(),
        table.column("roi").to_pylist(),
        table.column("text").to_pylist(),
        table.column("confidence").to_pylist(),
        table.column("parsed").to_pylist(),
        strict=True,
    ):
        rows.append(
            OcrRow(
                t=float(t),
                roi=str(roi),
                text=str(text),
                confidence=float(confidence),
                parsed=None if parsed is None else str(parsed),
            )
        )
    return rows


def ocr_result_dict(session_id: str, path: Path, rows: list[OcrRow], provider: OcrProvider) -> dict:
    return {
        "session_id": session_id,
        "path": str(path),
        "provider": provider.name,
        "model": provider.model,
        "rows": len(rows),
        "columns": ["t", "roi", "text", "confidence", "parsed"],
    }


def _rows_to_table(rows: list[OcrRow]):
    import pyarrow as pa

    return pa.table(
        {
            "t": pa.array([row.t for row in rows], type=pa.float64()),
            "roi": pa.array([row.roi for row in rows], type=pa.string()),
            "text": pa.array([row.text for row in rows], type=pa.string()),
            "confidence": pa.array([row.confidence for row in rows], type=pa.float64()),
            "parsed": pa.array([row.parsed for row in rows], type=pa.string()),
        }
    )


def _image_size(path: Path) -> tuple[int, int]:
    probe = media.ffprobe(path)
    for stream in probe.get("streams") or []:
        width, height = stream.get("width"), stream.get("height")
        if width and height:
            return int(width), int(height)
    raise MediaError(f"no video size for {path}")


def _frame_stem(image: Path) -> str:
    stem = image.stem
    for name in OCR_ROIS:
        suffix = f"_{name}"
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def _load_sidecar(image: Path, frame_stem: str) -> dict:
    names = (f"{frame_stem}.ocr.json", f"{image.stem}.ocr.json")
    for folder in (image.parent, image.parent.parent):
        for name in names:
            path = folder / name
            if path.is_file():
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if isinstance(raw, dict):
                    return raw
    return {}


def _parse_decimal(raw: str) -> float | None:
    if "," in raw and "." in raw:
        if raw.rfind(",") > raw.rfind("."):
            normalised = raw.replace(".", "").replace(",", ".")
        else:
            normalised = raw.replace(",", "")
    elif "," in raw:
        left, _, right = raw.partition(",")
        normalised = f"{left.replace('.', '')}.{right}" if len(right) in {1, 2} else raw.replace(",", "")
    else:
        normalised = raw
    try:
        return float(normalised)
    except ValueError:
        return None


def _format_float(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    if text in {"", "-", "-0"}:
        return "0"
    return text


def _paddleocr_version() -> str:
    try:
        from importlib.metadata import version

        return version("paddleocr")
    except Exception:
        return "unknown"


def _paddle_lines(engine: object, image: Path) -> tuple[list[str], list[float]]:
    texts: list[str] = []
    confs: list[float] = []
    raw = None
    if hasattr(engine, "ocr"):
        try:
            raw = engine.ocr(str(image), cls=True)  # type: ignore[attr-defined]
        except TypeError:
            raw = engine.ocr(str(image))  # type: ignore[attr-defined]
    if raw is None and hasattr(engine, "predict"):
        raw = engine.predict(str(image))  # type: ignore[attr-defined]
    for item in _flatten_paddle(raw):
        texts.append(item[0])
        confs.append(item[1])
    return texts, confs


def _flatten_paddle(raw: object) -> list[tuple[str, float]]:
    out: list[tuple[str, float]] = []
    if raw is None:
        return out
    if isinstance(raw, dict):
        rec = raw.get("rec_texts") or raw.get("text") or []
        scores = raw.get("rec_scores") or raw.get("confidence") or []
        if isinstance(rec, str):
            rec = [rec]
        if isinstance(scores, (int, float)):
            scores = [float(scores)]
        for text, score in zip(list(rec), list(scores) or [1.0] * len(list(rec)), strict=False):
            out.append((str(text), float(score)))
        return out
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], (list, tuple)):
                out.append((str(item[1][0]), float(item[1][1])))
            elif isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], str):
                out.append((str(item[0]), float(item[1])))
            else:
                out.extend(_flatten_paddle(item))
    return out
