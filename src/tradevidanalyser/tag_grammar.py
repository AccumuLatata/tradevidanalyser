"""JX0-A1 tag grammar from ThesisTester ``tag_map.yaml`` (pin 4fc4af05).

Parser matches ``tests/test_tag_map_schema.py::_grammar_errors`` /
``_ma_pair_ok`` at that SHA. Date gate is ``tag_grammar.effective_from``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

CHARSET = re.compile(r"^[A-Za-z0-9_]+$")


def grammar_effective_from(payload: Mapping[str, Any] | None) -> date | None:
    if not payload:
        return None
    grammar = payload.get("tag_grammar")
    if not isinstance(grammar, Mapping):
        return None
    raw = grammar.get("effective_from")
    if not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def grammar_active(payload: Mapping[str, Any] | None, as_of: date | None) -> bool:
    """True only when *as_of* is on/after the YAML cutoff. ``as_of=None`` is off."""
    if as_of is None:
        return False
    cutoff = grammar_effective_from(payload)
    if cutoff is None:
        return False
    return as_of >= cutoff


def grammar_closed_tags(payload: Mapping[str, Any]) -> frozenset[str]:
    """Closed prefixed tokens (not open ``ma_pair`` variants)."""
    grammar = payload.get("tag_grammar")
    if not isinstance(grammar, Mapping):
        return frozenset()
    tags: set[str] = set()
    playbooks = payload.get("playbooks") or {}
    if isinstance(playbooks, Mapping):
        for key, row in playbooks.items():
            tags.add(f"PB_{key}")
            if isinstance(row, Mapping):
                for alias in row.get("renamed_from") or ():
                    tags.add(f"PB_{alias}")
    tags.update(str(item) for item in (grammar.get("entry_values") or ()) if item)
    tags.update(str(item) for item in (grammar.get("variant_families") or {}) if item)
    none = grammar.get("confluence_none")
    if none:
        tags.add(str(none))
    triggers = payload.get("triggers") or {}
    if isinstance(triggers, Mapping):
        for key in triggers:
            tags.add(f"T_{key}")
    tags.update(str(item) for item in (grammar.get("control_tags") or {}) if item)
    return frozenset(tags)


def ma_pair_ok(variant: str, payload: Mapping[str, Any]) -> bool:
    """TT ``_ma_pair_ok``: both legs are confirm desk keys and len1 < len2."""
    grammar = payload.get("tag_grammar")
    if not isinstance(grammar, Mapping):
        return False
    patterns = grammar.get("variant_patterns")
    if not isinstance(patterns, Mapping):
        return False
    pattern = patterns.get("ma_pair")
    if not isinstance(pattern, str):
        return False
    match = re.match(pattern, variant)
    if not match:
        return False
    timeframe, len1, kind1, len2, kind2 = match.groups()
    exact = payload.get("exact")
    for key in (f"{timeframe}{len1}{kind1}", f"{timeframe}{len2}{kind2}"):
        row = exact.get(key) if isinstance(exact, Mapping) else None
        if not (isinstance(row, dict) and row.get("class") == "confirm"):
            return False
    return int(len1) < int(len2)


def resolve_grammar_token(token: str, payload: Mapping[str, Any]) -> str | None:
    """Return the canonical prefixed tag if *token* is a closed or ma_pair tag."""
    raw = (token or "").strip()
    if not raw:
        return None
    closed = grammar_closed_tags(payload)
    by_fold = {tag.casefold(): tag for tag in closed}
    hit = by_fold.get(raw.casefold())
    if hit:
        return hit
    if ma_pair_ok(raw, payload):
        return raw
    return None


def grammar_errors(
    tags: set[str] | list[str], payload: Mapping[str, Any]
) -> list[tuple[str, object]]:
    """Reference validator from TT ``_grammar_errors`` at 4fc4af05."""
    grammar = payload["tag_grammar"]
    playbook_reg = payload["playbooks"]
    trigger_reg = payload["triggers"]
    vocab = (set(payload["exact"]) | set(payload["context"])) - set(
        grammar["legacy_nonconforming_context"]
    )
    errors: list[tuple[str, object]] = []

    def bad(code: str, tag: object) -> None:
        errors.append((code, tag))

    playbooks: list[str] = []
    entries: list[str] = []
    variants: list[str] = []
    triggers: list[str] = []
    c_nones: list[str] = []
    confluences: list[str] = []
    for tag in set(tags):
        if tag in grammar["control_tags"]:
            bad("reentry_unresolved", tag)
        elif tag.startswith("PB_"):
            if tag != "PB_":
                playbooks.append(tag)
            else:
                bad("unknown_reserved", tag)
        elif tag.startswith("T_"):
            if tag != "T_":
                triggers.append(tag)
            else:
                bad("unknown_reserved", tag)
        elif tag.startswith("E_"):
            if tag in grammar["entry_values"]:
                entries.append(tag)
            else:
                bad("unknown_reserved", tag)
        elif tag.startswith("C_"):
            if tag == grammar["confluence_none"]:
                c_nones.append(tag)
            else:
                bad("unknown_reserved", tag)
        elif tag.startswith("X_"):
            bad("unknown_reserved", tag)
        elif tag.startswith("V_"):
            variants.append(tag)
        else:
            confluences.append(tag)
    playbook = None
    if not playbooks:
        bad("playbook_missing", None)
    elif len(playbooks) > 1:
        bad("playbook_multiple", tuple(sorted(playbooks)))
    else:
        ident = playbooks[0][3:]
        alias_map = {
            alias: key for key, row in playbook_reg.items() for alias in row["renamed_from"]
        }
        ident = alias_map.get(ident, ident)
        if ident not in playbook_reg:
            bad("playbook_unknown", playbooks[0])
        elif playbook_reg[ident]["status"] == "inactive":
            bad("playbook_inactive", playbooks[0])
        else:
            playbook = playbook_reg[ident]
    if not entries:
        bad("entry_missing", None)
    elif len(entries) > 1:
        bad("entry_multiple", tuple(sorted(entries)))
    if len(variants) > 1:
        bad("variant_multiple", tuple(sorted(variants)))
    elif variants and playbook is not None:
        variant = variants[0]
        allowed = (
            ma_pair_ok(variant, payload)
            if playbook["variants_pattern"] == "ma_pair"
            else variant in playbook["variants"]
        )
        if not allowed:
            bad("variant_not_allowed", variant)
    if len(triggers) > 1:
        bad("trigger_multiple", tuple(sorted(triggers)))
    elif triggers:
        ident = triggers[0][2:]
        if ident not in trigger_reg:
            bad("trigger_unknown", triggers[0])
        elif trigger_reg[ident]["status"] == "inactive":
            bad("trigger_inactive", triggers[0])
    for tag in confluences:
        if not CHARSET.match(tag):
            bad("charset_violation", tag)
        elif not (
            tag in vocab
            or any(
                tag.endswith(suffix) and tag[: -len(suffix)] in vocab
                for suffix in grammar["qualifiers"]
            )
        ):
            bad("unknown_tag", tag)
    if c_nones and confluences:
        bad("c_none_with_confluence", None)
    if not c_nones and not confluences:
        bad("confluence_missing", None)
    return sorted(errors, key=lambda item: (item[0], str(item[1])))


def grammar_codes(tags: set[str] | list[str], payload: Mapping[str, Any]) -> list[str]:
    return sorted(code for code, _ in grammar_errors(tags, payload))
