"""Parse docs/GLOSSARY.md into ASR initial_prompt and token lists."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_INITIAL_PROMPT_TOKENS = 220

_HEADING = re.compile(r"^##\s+(.+?)\s*$")
_LEVELS_HEADING = re.compile(r"levels?\s*/\s*locations?", re.IGNORECASE)
_PLAYBOOK_HEADING = re.compile(r"playbooks?\s*/\s*rules?", re.IGNORECASE)
_PLATFORM_HEADING = re.compile(r"^platform\b", re.IGNORECASE)
_LIST_MARK = re.compile(r"^[-*+]\s+")
# Sentence / markdown wrapping around a list item — not part of the token.
_EDGE_PUNCT = ".,;:!?\"'“”„‚«»()[]{}"


@dataclass(frozen=True)
class Glossary:
    level_tokens: tuple[str, ...]
    playbook_terms: tuple[str, ...]
    platform_terms: tuple[str, ...]
    initial_prompt: str

    @property
    def tokens(self) -> tuple[str, ...]:
        """Jargon tokens in document order: levels, playbooks, platform."""
        seen: list[str] = []
        for token in (*self.level_tokens, *self.playbook_terms, *self.platform_terms):
            if token not in seen:
                seen.append(token)
        return tuple(seen)


def default_glossary_path() -> Path:
    here = Path(__file__).resolve()
    candidates = [
        here.parents[2] / "docs" / "GLOSSARY.md",
        Path.cwd() / "docs" / "GLOSSARY.md",
    ]
    current = Path.cwd()
    for _ in range(6):
        candidates.append(current / "docs" / "GLOSSARY.md")
        if current.parent == current:
            break
        current = current.parent
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve() if path.exists() else path
        if resolved in seen:
            continue
        seen.add(resolved)
        if path.is_file():
            return path
    raise FileNotFoundError("docs/GLOSSARY.md not found")


def _classify_heading(title: str) -> str | None:
    if _LEVELS_HEADING.search(title):
        return "levels"
    if _PLAYBOOK_HEADING.search(title):
        return "playbooks"
    if _PLATFORM_HEADING.search(title):
        return "platform"
    return None


def _clean_token(raw: str) -> str:
    token = raw.strip()
    token = _LIST_MARK.sub("", token)
    token = token.strip("*_`")
    token = token.strip(_EDGE_PUNCT)
    token = " ".join(token.split())
    if not token or token.startswith("#"):
        return ""
    return token


def _split_tokens(body: str) -> tuple[str, ...]:
    parts: list[str] = []
    seen: set[str] = set()
    for raw in re.split(r"[\n,]", body):
        token = _clean_token(raw)
        if not token or token in seen:
            continue
        seen.add(token)
        parts.append(token)
    return tuple(parts)


def _prompt_words(*groups: tuple[str, ...]) -> str:
    words: list[str] = []
    for group in groups:
        for token in group:
            for piece in token.split():
                if len(words) >= MAX_INITIAL_PROMPT_TOKENS:
                    return " ".join(words)
                words.append(piece)
    return " ".join(words)


def parse_glossary(text: str) -> Glossary:
    """Deterministic parse of GLOSSARY.md section bodies into token tuples."""
    sections: dict[str, list[str]] = {"levels": [], "playbooks": [], "platform": []}
    current: str | None = None
    body_lines: list[str] = []

    def flush() -> None:
        nonlocal body_lines
        if current is not None:
            sections[current].extend(_split_tokens("\n".join(body_lines)))
        body_lines = []

    for line in text.splitlines():
        heading = _HEADING.match(line)
        if heading:
            flush()
            current = _classify_heading(heading.group(1))
            continue
        if current is not None:
            body_lines.append(line)
    flush()

    levels = tuple(sections["levels"])
    playbooks = tuple(sections["playbooks"])
    platform = tuple(sections["platform"])
    return Glossary(
        level_tokens=levels,
        playbook_terms=playbooks,
        platform_terms=platform,
        initial_prompt=_prompt_words(levels, playbooks, platform),
    )


def load_glossary(path: str | Path | None = None) -> Glossary:
    # No process-wide cache: tva serve would otherwise keep a stale parse after
    # GLOSSARY.md is edited. The file is small enough to read on each call.
    resolved = Path(path) if path else default_glossary_path()
    return parse_glossary(resolved.read_text(encoding="utf-8"))
