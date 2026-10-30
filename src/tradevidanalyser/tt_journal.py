"""Resolve ThesisTester JX1 / TJ4 / JX2 / JX8 by the cited source, not by guessed names.

Does not reimplement cost math or the position builder. When the private
package is absent, callers get the loud ``TT_*_API_MISSING`` errors.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from typing import Any

TT_TOPSTEPX_MODULE = "thesistester.journal.topstepx_reconcile"
TT_AMP_MODULE = "thesistester.journal.reconcile"
TT_POSITION_MODULE = "thesistester.journal.positions"
TT_LOCK_MODULE = "thesistester.journal.lock"

# User-cited spans on ThesisTester d32f6d3.
JX1_LINES = (1152, 1184)
TJ4_LINES = (422, 480)
JX8_LINES = (2195, 2219)
JX2_POS_ID_MARKERS = ("pos:", "%Y%m%dT%H%M%SZ")

TT_COST_API_MISSING = (
    "ThesisTester JX1/TJ4 cost function fehlt "
    "(topstepx_reconcile.py:1152-1184 or reconcile.py:422-480)"
)
TT_POSITION_API_MISSING = (
    "ThesisTester JX2 position builder fehlt (positions.py; "
    "id pos:{account}:{contract}:{open_ts:%Y%m%dT%H%M%SZ})"
)
TT_LOCK_API_MISSING = (
    "ThesisTester JX8 week-lock verify fehlt (lock.py:2195-2219)"
)


def _public(mod: Any) -> list[str]:
    return sorted(name for name in dir(mod) if not name.startswith("_"))


def _import_module(name: str) -> Any | None:
    try:
        return importlib.import_module(name)
    except ImportError:
        return None


def _callables(mod: Any) -> list[tuple[str, Callable[..., Any]]]:
    found: list[tuple[str, Callable[..., Any]]] = []
    for name, obj in inspect.getmembers(mod):
        if inspect.isfunction(obj):
            found.append((name, obj))
        elif inspect.isclass(obj) and getattr(obj, "__module__", "") == getattr(
            mod, "__name__", ""
        ):
            for meth_name, meth in inspect.getmembers(obj, predicate=inspect.isfunction):
                found.append((f"{name}.{meth_name}", meth))
    return found


def _covers_lines(fn: Callable[..., Any], start: int, end: int) -> bool:
    try:
        lines, lineno = inspect.getsourcelines(fn)
    except (OSError, TypeError):
        return False
    last = lineno + len(lines) - 1
    return lineno <= end and last >= start


def _function_covering_lines(
    mod: Any, start: int, end: int
) -> tuple[str, Callable[..., Any]] | None:
    matches: list[tuple[int, str, Callable[..., Any]]] = []
    for name, fn in _callables(mod):
        if not _covers_lines(fn, start, end):
            continue
        try:
            _src, lineno = inspect.getsourcelines(fn)
        except (OSError, TypeError):
            continue
        span = len(_src)
        matches.append((span, name, fn))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    _span, name, fn = matches[0]
    return name, fn


def _source(obj: Any) -> str:
    try:
        return inspect.getsource(obj)
    except (OSError, TypeError):
        return ""


def load_tt_cost_functions() -> list[tuple[str, Callable[..., Any]]]:
    """Return the JX1 and TJ4 functions at the cited line ranges, if importable."""
    found: list[tuple[str, Callable[..., Any]]] = []
    for module_name, span in (
        (TT_TOPSTEPX_MODULE, JX1_LINES),
        (TT_AMP_MODULE, TJ4_LINES),
    ):
        mod = _import_module(module_name)
        if mod is None:
            continue
        match = _function_covering_lines(mod, *span)
        if match is not None:
            name, fn = match
            found.append((f"{module_name}.{name}", fn))
    return found


def load_tt_position_builder() -> Callable[..., Any]:
    """Return the JX2 builder that stamps ``pos:{account}:{contract}:{open_ts}``."""
    mod = _import_module(TT_POSITION_MODULE)
    if mod is None:
        raise ImportError(TT_POSITION_API_MISSING)
    matches: list[tuple[str, Callable[..., Any]]] = []
    for name, fn in _callables(mod):
        src = _source(fn)
        if src and all(marker in src for marker in JX2_POS_ID_MARKERS):
            matches.append((name, fn))
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        matches.sort(key=lambda item: len(_source(item[1])))
        return matches[0][1]
    raise ImportError(f"{TT_POSITION_API_MISSING}; public={_public(mod)}")


def load_tt_week_verify() -> Callable[..., Any]:
    """Return the JX8 report/verify function at lock.py:2195-2219."""
    mod = _import_module(TT_LOCK_MODULE)
    if mod is None:
        raise ImportError(TT_LOCK_API_MISSING)
    match = _function_covering_lines(mod, *JX8_LINES)
    if match is None:
        raise ImportError(f"{TT_LOCK_API_MISSING}; public={_public(mod)}")
    return match[1]


def call_tt_week_verify(
    fn: Callable[..., Any],
    path: Any,
    *,
    week: str,
    account: str,
) -> Any:
    """Call JX8 with only parameters the function actually declares."""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{TT_LOCK_API_MISSING}: signature {fn!r}") from exc
    kwargs: dict[str, Any] = {}
    positional: list[Any] = []
    for name, param in sig.parameters.items():
        if name in {"self", "cls"}:
            continue
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        if name in {"path", "manifest_path", "week_manifest", "manifest"}:
            value: Any = path
        elif name == "week":
            value = week
        elif name == "account":
            value = account
        elif name in {"root", "journal_root"}:
            value = path.parent
        elif param.default is inspect.Parameter.empty:
            raise TypeError(f"{TT_LOCK_API_MISSING}: required param {name!r} on {fn!r}")
        else:
            continue
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            kwargs[name] = value
        else:
            positional.append(value)
    return fn(*positional, **kwargs)


def tt_cost_apis_available() -> bool:
    return bool(load_tt_cost_functions())


def tt_position_api_available() -> bool:
    try:
        load_tt_position_builder()
    except ImportError:
        return False
    return True


def tt_lock_api_available() -> bool:
    try:
        load_tt_week_verify()
    except ImportError:
        return False
    return True
