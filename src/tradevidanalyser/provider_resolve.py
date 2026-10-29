"""Resolve a provider name. Unset is an error; fake is explicit opt-in only."""

from __future__ import annotations

import os
import warnings

ENV_ASR_PROVIDER = "TVA_ASR_PROVIDER"
ENV_OCR_PROVIDER = "TVA_OCR_PROVIDER"
ENV_EXTRACT_PROVIDER = "TVA_EXTRACT_PROVIDER"

FAKE_ALIASES = frozenset({"fake", "test"})


def missing_provider_error(env: str) -> str:
    return f"{env} fehlt"


def fake_provider_warning(env: str, chosen: str) -> str:
    return f"{env}={chosen} (explizit gesetzt)"


def require_provider_name(
    name: str | None,
    *,
    env: str,
    error_cls: type[Exception],
    extra_fake: frozenset[str] = frozenset(),
) -> str:
    """Return a chosen provider name or raise ``error_cls`` if unset/empty.

    ``fake`` / ``test`` (and any ``extra_fake`` aliases) are allowed only when
    the caller or the environment set them. A warning is emitted so the opt-in
    is visible.
    """
    supplied = name if name is not None and str(name).strip() else None
    raw = supplied if supplied is not None else os.environ.get(env)
    chosen = (raw or "").strip().lower()
    if not chosen:
        raise error_cls(missing_provider_error(env))
    if chosen in FAKE_ALIASES | extra_fake:
        warnings.warn(fake_provider_warning(env, chosen), UserWarning, stacklevel=3)
    return chosen
