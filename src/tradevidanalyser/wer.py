"""Word error rate and jargon recall for German session tapes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import TypedDict

from tradevidanalyser.glossary import load_glossary
from tradevidanalyser.schema import Transcript

_PUNCT = re.compile(r"[^\w\s\-]", re.UNICODE)
_WS = re.compile(r"\s+")

_UMLAUT_FOLD = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
        "Ä": "ae",
        "Ö": "oe",
        "Ü": "ue",
    }
)


class WerReport(TypedDict):
    wer: float
    jargon_recall: float
    n_words: int


def normalize_german(text: str, *, fold_umlauts: bool = False) -> str:
    """Lowercase and strip punctuation. Umlaut folding is off by default."""
    if fold_umlauts:
        text = text.translate(_UMLAUT_FOLD)
    text = text.casefold()
    text = _PUNCT.sub(" ", text)
    return _WS.sub(" ", text).strip()


def tokenize(text: str, *, fold_umlauts: bool = False) -> list[str]:
    return [w for w in normalize_german(text, fold_umlauts=fold_umlauts).split() if w]


def n_words(text: str, *, fold_umlauts: bool = False) -> int:
    return len(tokenize(text, fold_umlauts=fold_umlauts))


def _edit_counts(ref: list[str], hyp: list[str]) -> tuple[int, int, int]:
    """Levenshtein alignment → (substitutions, deletions, insertions)."""
    n, m = len(ref), len(hyp)
    prev = [(j, 0, 0, j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [(i, 0, i, 0)] + [(0, 0, 0, 0)] * m
        for j in range(1, m + 1):
            if ref[i - 1] == hyp[j - 1]:
                eq = prev[j - 1]
            else:
                d, s, dele, ins = prev[j - 1]
                eq = (d + 1, s + 1, dele, ins)
            d, s, dele, ins = prev[j]
            delete = (d + 1, s, dele + 1, ins)
            d, s, dele, ins = cur[j - 1]
            insert = (d + 1, s, dele, ins + 1)
            cur[j] = min(eq, delete, insert, key=lambda row: row[0])
        prev = cur
    _, subst, dele, ins = prev[m]
    return subst, dele, ins


def word_error_rate(ref: str, hyp: str, *, fold_umlauts: bool = False) -> float:
    r_words = tokenize(ref, fold_umlauts=fold_umlauts)
    h_words = tokenize(hyp, fold_umlauts=fold_umlauts)
    if not r_words:
        return 0.0 if not h_words else 1.0
    subst, dele, ins = _edit_counts(r_words, h_words)
    return (subst + dele + ins) / len(r_words)


def _contains_token(text: str, token: str, *, fold_umlauts: bool = False) -> bool:
    hay = normalize_german(text, fold_umlauts=fold_umlauts)
    needle = normalize_german(token, fold_umlauts=fold_umlauts)
    if not needle:
        return False
    pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
    return re.search(pattern, hay) is not None


def jargon_recall(
    ref: str,
    hyp: str,
    tokens: Sequence[str],
    *,
    fold_umlauts: bool = False,
) -> float:
    """Fraction of jargon tokens present in *ref* that also appear in *hyp*."""
    present = [tok for tok in tokens if _contains_token(ref, tok, fold_umlauts=fold_umlauts)]
    if not present:
        return 1.0
    hits = sum(1 for tok in present if _contains_token(hyp, tok, fold_umlauts=fold_umlauts))
    return hits / len(present)


def hyp_from_transcript(transcript: Transcript) -> str:
    return " ".join(seg.text for seg in transcript.segments)


def score_texts(
    ref: str,
    hyp: str,
    tokens: Sequence[str] | None = None,
    *,
    fold_umlauts: bool = False,
) -> WerReport:
    chosen = tuple(tokens) if tokens is not None else load_glossary().tokens
    return {
        "wer": word_error_rate(ref, hyp, fold_umlauts=fold_umlauts),
        "jargon_recall": jargon_recall(ref, hyp, chosen, fold_umlauts=fold_umlauts),
        "n_words": n_words(ref, fold_umlauts=fold_umlauts),
    }


def score_session(session_id: str, *, ref: Path, root: Path) -> WerReport:
    from tradevidanalyser import store

    transcript = store.load_transcript(root, session_id)
    return score_texts(ref.read_text(encoding="utf-8"), hyp_from_transcript(transcript))
