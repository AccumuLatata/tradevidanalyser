"""Opt-in VLM notes. Off unless TVA_VLM_PROVIDER is set. CI uses fake + mocks."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from tradevidanalyser import store
from tradevidanalyser.ocr import OcrRow, read_ocr_parquet
from tradevidanalyser.providers.extract import default_prompt_path, prompt_version_for
from tradevidanalyser.schema import VisualNote

ENV_VLM_PROVIDER = "TVA_VLM_PROVIDER"
ENV_VLM_MODEL = "TVA_VLM_MODEL"
ENV_XAI_KEY = "XAI_API_KEY"
ENV_GEMINI_KEY = "GEMINI_API_KEY"
ENV_GOOGLE_KEY = "GOOGLE_API_KEY"

DEFAULT_GROK_MODEL = "grok-4.6"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
PROMPT_FILENAME = "vlm_v1.md"
XAI_CHAT_URL = "https://api.x.ai/v1/chat/completions"
GEMINI_GENERATE_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_FILES_URL = "https://generativelanguage.googleapis.com/upload/v1beta/files"
GEMINI_FILE_URL = "https://generativelanguage.googleapis.com/v1beta/{name}"
HTTP_TIMEOUT = 3600.0
MAX_FRAMES = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
GEMINI_FILE_POLL_S = 0.25
GEMINI_FILE_POLL_ATTEMPTS = 40
VISUAL_NOTE_GAP_PREFIX = "dropped visual_notes:"

_DIGIT_RUN = re.compile(r"\d+")
_OFF = frozenset({"", "off", "none", "disabled"})


class VlmError(RuntimeError):
    pass


@dataclass
class VlmContext:
    session_id: str
    frames: list[Path]
    clips: list[Path]
    ocr_rows: list[OcrRow]
    recording_path: str | None = None
    root: Path | None = None


@dataclass
class VlmResult:
    notes: list[VisualNote]
    cost_usd: float = 0.0
    model: str = ""
    prompt_version: str = ""
    frames_used: list[str] = field(default_factory=list)
    clip: str | None = None


class VlmProvider(Protocol):
    name: str
    model: str
    last_cost_usd: float

    def annotate(self, ctx: VlmContext) -> VlmResult: ...


def ocr_digit_runs(rows: list[OcrRow]) -> set[str]:
    allowed: set[str] = set()
    for row in rows:
        for value in (row.text, row.parsed):
            if value:
                allowed.update(_DIGIT_RUN.findall(value))
    return allowed


def visual_note_problems(
    notes: list[VisualNote],
    *,
    ocr_rows: list[OcrRow],
    frame_stems: set[str],
    clip_names: set[str],
) -> list[tuple[VisualNote, str]]:
    """Return (note, reason) for notes the number / citation guard rejects."""
    allowed = ocr_digit_runs(ocr_rows)
    problems: list[tuple[VisualNote, str]] = []
    for note in notes:
        text = note.text or ""
        if not text.strip():
            problems.append((note, "empty visual note"))
            continue
        for run in _DIGIT_RUN.findall(text):
            if run not in allowed:
                problems.append((note, f"visual note digit {run} not in ocr.parquet"))
                break
        else:
            bad_frame = next((stem for stem in note.frames_cited if stem not in frame_stems), None)
            if bad_frame is not None:
                problems.append((note, f"frames_cited {bad_frame!r} is not a saved frame"))
                continue
            if note.clip and note.clip not in clip_names:
                problems.append((note, f"clip {note.clip!r} is not a saved clip"))
    return problems


def apply_visual_note_guard(
    notes: list[VisualNote],
    *,
    ocr_rows: list[OcrRow],
    frame_stems: set[str],
    clip_names: set[str],
) -> tuple[list[VisualNote], list[str]]:
    """Drop notes that invent OCR-absent numbers or cite missing frames/clips."""
    problems = visual_note_problems(
        notes, ocr_rows=ocr_rows, frame_stems=frame_stems, clip_names=clip_names
    )
    if not problems:
        return list(notes), []
    drop = {id(note) for note, _reason in problems}
    gaps = [f"dropped visual_notes: {reason}" for _note, reason in problems]
    kept = [note for note in notes if id(note) not in drop]
    return kept, gaps


def merge_visual_note_gaps(existing: list[str], gaps: list[str]) -> list[str]:
    """Replace prior visual-note drop reasons so retries do not stack forever."""
    kept = [item for item in existing if not item.startswith(VISUAL_NOTE_GAP_PREFIX)]
    return sorted(set(kept) | set(gaps))


def notes_json_schema() -> dict[str, Any]:
    """xAI strict json_schema: every object is closed and every key is required."""
    note = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "text": {"type": "string"},
            "t": {"type": ["number", "null"]},
            "frames_cited": {"type": "array", "items": {"type": "string"}},
            "clip": {"type": ["string", "null"]},
        },
        "required": ["text", "t", "frames_cited", "clip"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"notes": {"type": "array", "items": note}},
        "required": ["notes"],
    }


def _load_ocr_rows(root: Path, session_id: str) -> list[OcrRow]:
    path = store.ocr_path(root, session_id)
    if not path.is_file():
        return []
    return read_ocr_parquet(path)


def _format_ocr_block(rows: list[OcrRow]) -> str:
    if not rows:
        return (
            "OCR reads: none. Write no numerals (digits) in note text. "
            "Cite frames via frames_cited only."
        )
    lines = [
        "OCR reads — these are the only numerals you may write in note text:",
    ]
    for row in rows:
        parsed = row.parsed if row.parsed is not None else ""
        lines.append(f"- t={row.t:.3f} roi={row.roi} text={row.text!r} parsed={parsed!r}")
    return "\n".join(lines)


def _user_prompt(ctx: VlmContext, frame_stems: list[str], clip_name: str | None) -> str:
    lines = [
        "Redacted desk frames and/or a redacted chapter clip only.",
        "Describe layout and qualitative structure. Do not invent prices.",
        _format_ocr_block(ctx.ocr_rows),
        f"Attached frame stems: {', '.join(frame_stems) if frame_stems else '(none)'}.",
    ]
    if clip_name:
        lines.append(f"Attached clip filename: {clip_name}.")
    lines.append("Return JSON {\"notes\": [{\"text\", \"t\", \"frames_cited\", \"clip\"}]}.")
    return "\n".join(lines)


def _usable_frames(paths: list[Path]) -> list[Path]:
    usable: list[Path] = []
    for path in paths:
        if not path.is_file():
            continue
        if path.stat().st_size == 0 or path.stat().st_size > MAX_IMAGE_BYTES:
            continue
        usable.append(path)
        if len(usable) >= MAX_FRAMES:
            break
    return usable


def _jpeg_data_url(path: Path) -> str:
    raw = path.read_bytes()
    return "data:image/jpeg;base64," + base64.standard_b64encode(raw).decode("ascii")


def _message_content(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _json_object(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        body = json.loads(text)
    except json.JSONDecodeError as exc:
        raise VlmError("VLM message content is not JSON") from exc
    if not isinstance(body, dict):
        raise VlmError("VLM JSON content is not an object")
    return body


def notes_from_payload(payload: dict[str, Any]) -> list[VisualNote]:
    if not isinstance(payload, dict):
        raise VlmError("VLM returned a non-object JSON body")
    notes_raw = payload.get("notes")
    if notes_raw is None and "choices" in payload:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise VlmError("Grok response has no choices")
        first = choices[0]
        if not isinstance(first, dict):
            raise VlmError("Grok choice is not an object")
        message = first.get("message")
        if not isinstance(message, dict):
            raise VlmError("Grok choice has no message")
        content = message.get("content")
        if isinstance(content, dict):
            payload = content
        else:
            raw = _message_content(message).strip()
            if not raw:
                refusal = message.get("refusal")
                if refusal:
                    raise VlmError(f"Grok refused: {refusal}")
                raise VlmError("Grok message content is empty")
            payload = _json_object(raw)
        notes_raw = payload.get("notes")
    if notes_raw is None and "candidates" in payload:
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise VlmError("Gemini response has no candidates")
        first = candidates[0]
        if not isinstance(first, dict):
            raise VlmError("Gemini candidate is not an object")
        content = first.get("content")
        if not isinstance(content, dict):
            raise VlmError("Gemini candidate has no content")
        parts = content.get("parts")
        texts: list[str] = []
        if isinstance(parts, list):
            for part in parts:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
        if not texts:
            raise VlmError("Gemini message content is empty")
        payload = _json_object("".join(texts))
        notes_raw = payload.get("notes")
    if not isinstance(notes_raw, list):
        raise VlmError("VLM JSON has no notes array")
    notes: list[VisualNote] = []
    for item in notes_raw:
        try:
            notes.append(VisualNote.model_validate(item))
        except (TypeError, ValueError) as exc:
            raise VlmError(f"VLM note did not match VisualNote: {exc}") from exc
    return notes


def _xai_cost_usd(payload: dict[str, Any]) -> float:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0.0
    prompt = float(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = float(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return (prompt * 1.25 + completion * 2.50) / 1_000_000.0


def _gemini_cost_usd(payload: dict[str, Any]) -> float:
    meta = payload.get("usageMetadata")
    if not isinstance(meta, dict):
        return 0.0
    prompt = float(meta.get("promptTokenCount") or 0)
    completion = float(meta.get("candidatesTokenCount") or 0)
    return (prompt * 0.15 + completion * 0.60) / 1_000_000.0


def _gemini_api_key() -> str:
    return (os.environ.get(ENV_GEMINI_KEY) or os.environ.get(ENV_GOOGLE_KEY) or "").strip()


def _assert_no_raw_tape(values: list[str], recording_path: str | None) -> None:
    """Refuse to send the original recording path anywhere in a hosted payload."""
    if not recording_path:
        return
    needle = Path(recording_path).name
    if not needle:
        return
    for value in values:
        if needle in value:
            raise VlmError("refusing to send the raw recording path to a VLM")


class FakeVlmProvider:
    """Qualitative placeholder. Never invents a price."""

    name = "fake"
    model = "fake-vlm-v1"
    last_cost_usd = 0.0

    def __init__(self, notes: list[VisualNote] | None = None) -> None:
        self._notes = notes

    def annotate(self, ctx: VlmContext) -> VlmResult:
        frames = _usable_frames(ctx.frames)
        clip = ctx.clips[0].name if ctx.clips else None
        if self._notes is not None:
            notes = list(self._notes)
        elif frames:
            stem = frames[0].stem
            notes = [
                VisualNote(
                    text="DOM is visible in the left pane at this chapter mark.",
                    t=float(stem),
                    frames_cited=[stem],
                    clip=clip,
                )
            ]
        elif clip:
            notes = [
                VisualNote(
                    text="Redacted chapter clip is available for a qualitative look.",
                    clip=clip,
                )
            ]
        else:
            notes = []
        self.last_cost_usd = 0.0
        return VlmResult(
            notes=notes,
            cost_usd=0.0,
            model=self.model,
            prompt_version="fake",
            frames_used=[path.stem for path in frames],
            clip=clip,
        )


class GrokVlmProvider:
    """xAI image understanding on redacted JPEGs. No video_url — Imagine is generation."""

    name = "grok"
    model = DEFAULT_GROK_MODEL
    last_cost_usd = 0.0

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self._client = client
        self.prompt_path = prompt_path
        env_model = (os.environ.get(ENV_VLM_MODEL) or "").strip()
        if env_model:
            self.model = env_model

    def annotate(self, ctx: VlmContext) -> VlmResult:
        key = (os.environ.get(ENV_XAI_KEY) or "").strip()
        if not key:
            raise VlmError("XAI_API_KEY is unset. Set it or use TVA_VLM_PROVIDER=fake.")
        frames = _usable_frames(ctx.frames)
        if not frames:
            raise VlmError(
                "grok vlm requires redacted frames; docs.x.ai has image understanding "
                "(image_url), not clip video understanding"
            )
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        system_prompt = path.read_text(encoding="utf-8")
        version = prompt_version_for(path)
        stems = [frame.stem for frame in frames]
        user_prompt = _user_prompt(ctx, stems, None)
        _assert_no_raw_tape(
            [system_prompt, user_prompt, *(str(frame) for frame in frames)],
            ctx.recording_path,
        )
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for frame in frames:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": _jpeg_data_url(frame), "detail": "high"},
                }
            )
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "visual_notes",
                    "strict": True,
                    "schema": notes_json_schema(),
                },
            },
        }
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
        owns = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        try:
            try:
                response = client.post(
                    XAI_CHAT_URL, json=body, headers=headers, timeout=HTTP_TIMEOUT
                )
            except httpx.HTTPError as exc:
                raise VlmError(f"Grok request failed: {exc}") from exc
        finally:
            if owns:
                client.close()
        if response.status_code >= 400:
            raise VlmError(f"Grok HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise VlmError("Grok returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise VlmError("Grok returned a non-object JSON body")
        notes = notes_from_payload(payload)
        cost = _xai_cost_usd(payload)
        self.last_cost_usd = cost
        return VlmResult(
            notes=notes,
            cost_usd=cost,
            model=self.model,
            prompt_version=version,
            frames_used=stems,
            clip=None,
        )


class GeminiVlmProvider:
    """Gemini File API for a redacted clip, or inline JPEG frames."""

    name = "gemini"
    model = DEFAULT_GEMINI_MODEL
    last_cost_usd = 0.0

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self._client = client
        self.prompt_path = prompt_path
        env_model = (os.environ.get(ENV_VLM_MODEL) or "").strip()
        if env_model:
            self.model = env_model

    def annotate(self, ctx: VlmContext) -> VlmResult:
        key = _gemini_api_key()
        if not key:
            raise VlmError("GEMINI_API_KEY is unset. Set it or use TVA_VLM_PROVIDER=fake.")
        frames = _usable_frames(ctx.frames)
        clip_path = ctx.clips[0] if ctx.clips else None
        if not frames and clip_path is None:
            raise VlmError("gemini vlm requires redacted frames or a redacted clip")
        path = self.prompt_path or default_prompt_path(PROMPT_FILENAME)
        system_prompt = path.read_text(encoding="utf-8")
        version = prompt_version_for(path)
        stems = [frame.stem for frame in frames]
        clip_name = clip_path.name if clip_path is not None and not frames else None
        user_prompt = _user_prompt(ctx, stems, clip_name)
        _assert_no_raw_tape(
            [
                system_prompt,
                user_prompt,
                *(str(frame) for frame in frames),
                str(clip_path) if clip_path is not None else "",
            ],
            ctx.recording_path,
        )
        parts: list[dict[str, Any]] = [
            {"text": system_prompt + "\n\n" + user_prompt},
        ]
        owns = self._client is None
        client = self._client or httpx.Client(timeout=HTTP_TIMEOUT)
        scratch: Path | None = None
        try:
            if frames:
                for frame in frames:
                    parts.append(
                        {
                            "inline_data": {
                                "mime_type": "image/jpeg",
                                "data": base64.standard_b64encode(frame.read_bytes()).decode(
                                    "ascii"
                                ),
                            }
                        }
                    )
            elif clip_path is not None:
                safe_clip = _redacted_clip_copy(
                    clip_path, ctx.recording_path, root=ctx.root
                )
                if safe_clip.resolve() != clip_path.resolve():
                    scratch = safe_clip
                uploaded = self._upload_clip(client, key, safe_clip)
                parts.append(
                    {
                        "file_data": {
                            "mime_type": "video/mp4",
                            "file_uri": uploaded,
                        }
                    }
                )
            url = GEMINI_GENERATE_URL.format(model=self.model)
            try:
                response = client.post(
                    url,
                    params={"key": key},
                    json={
                        "contents": [{"role": "user", "parts": parts}],
                        "generationConfig": {"responseMimeType": "application/json"},
                    },
                    timeout=HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise VlmError(f"Gemini request failed: {exc}") from exc
        finally:
            _cleanup_vlm_scratch(scratch)
            if owns:
                client.close()
        if response.status_code >= 400:
            raise VlmError(f"Gemini HTTP {response.status_code}: {response.text[:300]}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise VlmError("Gemini returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise VlmError("Gemini returned a non-object JSON body")
        notes = notes_from_payload(payload)
        cost = _gemini_cost_usd(payload)
        self.last_cost_usd = cost
        return VlmResult(
            notes=notes,
            cost_usd=cost,
            model=self.model,
            prompt_version=version,
            frames_used=stems,
            clip=clip_name,
        )

    def _upload_clip(self, client: httpx.Client, key: str, clip: Path) -> str:
        data = clip.read_bytes()
        try:
            start = client.post(
                GEMINI_FILES_URL,
                params={"key": key},
                headers={
                    "X-Goog-Upload-Protocol": "resumable",
                    "X-Goog-Upload-Command": "start",
                    "X-Goog-Upload-Header-Content-Length": str(len(data)),
                    "X-Goog-Upload-Header-Content-Type": "video/mp4",
                    "Content-Type": "application/json",
                },
                json={"file": {"display_name": clip.name}},
                timeout=HTTP_TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise VlmError(f"Gemini file upload failed: {exc}") from exc
        if start.status_code >= 400:
            raise VlmError(f"Gemini upload HTTP {start.status_code}: {start.text[:300]}")
        upload_url = start.headers.get("x-goog-upload-url")
        if upload_url:
            try:
                uploaded = client.post(
                    upload_url,
                    headers={
                        "Content-Length": str(len(data)),
                        "X-Goog-Upload-Offset": "0",
                        "X-Goog-Upload-Command": "upload, finalize",
                    },
                    content=data,
                    timeout=HTTP_TIMEOUT,
                )
            except httpx.HTTPError as exc:
                raise VlmError(f"Gemini file upload failed: {exc}") from exc
            if uploaded.status_code >= 400:
                raise VlmError(
                    f"Gemini upload HTTP {uploaded.status_code}: {uploaded.text[:300]}"
                )
            body = _response_object(uploaded, "Gemini upload")
        else:
            body = _response_object(start, "Gemini upload")
        uri, name, state = _gemini_file_ref(body)
        if state == "FAILED":
            raise VlmError("Gemini file processing failed")
        if state == "ACTIVE" and uri:
            return uri
        return self._wait_file_active(client, key, uri=uri, name=name)

    def _wait_file_active(
        self,
        client: httpx.Client,
        key: str,
        *,
        uri: str | None,
        name: str | None,
    ) -> str:
        file_name = _gemini_file_name(name=name, uri=uri)
        if not file_name:
            if uri:
                return uri
            raise VlmError("Gemini upload response has no file uri")
        url = GEMINI_FILE_URL.format(name=file_name)
        last_uri = uri
        for _ in range(GEMINI_FILE_POLL_ATTEMPTS):
            try:
                response = client.get(url, params={"key": key}, timeout=HTTP_TIMEOUT)
            except httpx.HTTPError as exc:
                raise VlmError(f"Gemini file status failed: {exc}") from exc
            if response.status_code >= 400:
                raise VlmError(f"Gemini file HTTP {response.status_code}: {response.text[:300]}")
            payload = _response_object(response, "Gemini file")
            got_uri, _name, state = _gemini_file_ref(payload)
            if got_uri:
                last_uri = got_uri
            if state == "FAILED":
                raise VlmError("Gemini file processing failed")
            if state == "ACTIVE":
                if not last_uri:
                    last_uri = f"https://generativelanguage.googleapis.com/v1beta/{file_name}"
                return last_uri
            time.sleep(GEMINI_FILE_POLL_S)
        raise VlmError("Gemini file did not become ACTIVE")


def _response_object(response: httpx.Response, label: str) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise VlmError(f"{label} returned non-JSON") from exc
    if not isinstance(body, dict):
        raise VlmError(f"{label} returned a non-object JSON body")
    return body


def _gemini_file_ref(body: dict[str, Any]) -> tuple[str | None, str | None, str | None]:
    file_obj = body.get("file") if isinstance(body.get("file"), dict) else body
    if not isinstance(file_obj, dict):
        return None, None, None
    uri = file_obj.get("uri")
    name = file_obj.get("name")
    state = file_obj.get("state")
    return (
        uri if isinstance(uri, str) and uri else None,
        name if isinstance(name, str) and name else None,
        state.upper() if isinstance(state, str) and state else None,
    )


def _gemini_file_name(*, name: str | None, uri: str | None) -> str | None:
    if name:
        if name.startswith("files/"):
            return name
        if "/" not in name:
            return f"files/{name}"
        return name
    if not uri:
        return None
    marker = "/files/"
    if marker in uri:
        return "files/" + uri.split(marker, 1)[1].split("?", 1)[0]
    if uri.startswith("files/"):
        return uri
    return None


def _cleanup_vlm_scratch(path: Path | None) -> None:
    if path is None:
        return
    parent = path.parent
    path.unlink(missing_ok=True)
    if parent.name.startswith("tva-vlm-"):
        shutil.rmtree(parent, ignore_errors=True)


def _redacted_clip_copy(
    clip: Path, recording_path: str | None, *, root: Path | None = None
) -> Path:
    """Re-apply TVA_ROOT layout masks before a clip leaves the box."""
    _assert_no_raw_tape([str(clip.resolve())], recording_path)
    try:
        from tradevidanalyser.frames import get_layout
        from tradevidanalyser.redact import drawbox_filter
        from tradevidanalyser import media
    except Exception as exc:
        raise VlmError(f"cannot load redaction tools for Gemini clip: {exc}") from exc
    try:
        layout = get_layout(root=root)
    except (ValueError, OSError) as exc:
        raise VlmError(f"cannot load layout for clip redaction: {exc}") from exc
    vf = drawbox_filter(layout)
    if not vf:
        return clip
    binary = media.which("ffmpeg")
    if not binary:
        raise VlmError("ffmpeg not on PATH (needed to re-redact a clip for Gemini)")
    tmp_dir = Path(tempfile.mkdtemp(prefix="tva-vlm-"))
    tmp = tmp_dir / clip.name
    result = subprocess.run(
        [
            binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(clip),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(tmp),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
        _cleanup_vlm_scratch(tmp)
        raise VlmError(result.stderr.strip() or "ffmpeg re-redact clip failed")
    return tmp


def get_vlm_provider(name: str | None = None) -> VlmProvider | None:
    """Unset / off / none → skip. Fake is opt-in, not the implicit default."""
    raw = name if name is not None else os.environ.get(ENV_VLM_PROVIDER)
    if raw is None:
        return None
    chosen = raw.strip().lower()
    if chosen in _OFF:
        return None
    if chosen in {"fake", "test"}:
        return FakeVlmProvider()
    if chosen in {"grok", "xai"}:
        return GrokVlmProvider()
    if chosen in {"gemini", "google"}:
        return GeminiVlmProvider()
    raise VlmError(f"unknown vlm provider {chosen!r}")


def vlm_artifact(
    session_id: str,
    result: VlmResult,
    *,
    provider: str,
    gaps: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": "1",
        "session_id": session_id,
        "provider": provider,
        "model": result.model,
        "prompt_version": result.prompt_version,
        "notes": [note.model_dump(mode="json") for note in result.notes],
        "gaps": gaps,
        "cost_usd": result.cost_usd,
        "frames_used": result.frames_used,
        "clip": result.clip,
    }
