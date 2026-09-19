"""The handol-mux policy-tuple rules (SPEC §2.5 "Policy tuples", "Settings validation").

Pure and free of I/O, so the client checks every query with it before sending and the session can
use the same rules for early feedback when a setting changes. The rule order matches the browser
validator (``tuple.js`` ``problem()``), so both report the same first problem.
"""

from __future__ import annotations

import math
from typing import Any

from .errors import EngineError

#: The keys a policy tuple may carry, in the order they are checked.
TUPLE_KEYS: tuple[str, ...] = ("lambda_utility", "trust_mu", "fill_kappa", "min_p",
                               "distance_slope", "distance_floor", "distance_peak", "temperature")
DEFAULT_DISTANCE_FLOOR = 0.1
DEFAULT_DISTANCE_PEAK = 1.5
#: How a colour picks its engine move: sample the human distribution, or KataGo's first choice.
MOVE_STYLES: tuple[str, ...] = ("human", "katago")

__all__ = ["DEFAULT_DISTANCE_FLOOR", "DEFAULT_DISTANCE_PEAK", "MOVE_STYLES", "TUPLE_KEYS",
           "check_policies", "tuple_problem", "wire_tuple"]


def _number(value: Any) -> bool:
    """A finite int or float; booleans, None, strings and overflowing ints are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _has(tuple_: dict, key: str) -> bool:
    return tuple_.get(key) is not None


def tuple_problem(tuple_: Any, visits: Any) -> str | None:
    """The first problem with ``tuple_`` as the mux would judge it with ``visits``, or ``None``."""
    if not isinstance(tuple_, dict):
        return "a policy tuple must be a JSON object"
    for name in tuple_:
        if name not in TUPLE_KEYS:
            return f"unknown key {str(name)[:40]!r}"
    for name in TUPLE_KEYS:
        if name not in tuple_ or (name == "lambda_utility" and tuple_[name] is None):
            continue
        if not _number(tuple_[name]):
            return f"{name} must be a finite number"

    def above(name: str, bound: float, inclusive: bool = False) -> str | None:
        if not _has(tuple_, name):
            return None
        value = tuple_[name]
        if value >= bound if inclusive else value > bound:
            return None
        return f"{name} must be {'at least' if inclusive else 'greater than'} {bound:g}"

    found = (above("lambda_utility", 0) or above("trust_mu", 0)
             or above("fill_kappa", 0, inclusive=True))
    if not found and _has(tuple_, "min_p") and not 0 <= tuple_["min_p"] <= 1:
        found = "min_p must be between 0 and 1"
    found = (found or above("distance_slope", 0, inclusive=True) or above("distance_floor", 0)
             or above("distance_peak", 0) or above("temperature", 0.0001))
    if found:
        return found
    lam = _has(tuple_, "lambda_utility")
    if lam and not (_has(tuple_, "trust_mu") and _has(tuple_, "fill_kappa")):
        return "lambda_utility requires trust_mu and fill_kappa"
    if not lam and (_has(tuple_, "trust_mu") or _has(tuple_, "fill_kappa")):
        return "trust_mu and fill_kappa require lambda_utility"
    floor = tuple_["distance_floor"] if _has(tuple_, "distance_floor") else DEFAULT_DISTANCE_FLOOR
    peak = tuple_["distance_peak"] if _has(tuple_, "distance_peak") else DEFAULT_DISTANCE_PEAK
    if not floor < peak:
        return "distance_floor must be below distance_peak"
    if lam and not (_number(visits) and visits > 1):
        return "lambda_utility needs a search (max visits above 1)"
    return None


def check_policies(policies: Any, visits: Any) -> None:
    """Refuse anything but one or two valid tuples with an :class:`EngineError` (no address)."""
    if not isinstance(policies, list):
        raise EngineError("the policy tuples must be a list")
    if len(policies) not in (1, 2):
        raise EngineError(f"a query carries one policy tuple, or two to compare; "
                          f"got {len(policies)}")
    for index, tuple_ in enumerate(policies):
        problem = tuple_problem(tuple_, visits)
        if problem is not None:
            label = "policy tuple" if index == 0 else "compare tuple"
            raise EngineError(f"refused the {label}: {problem}")


def wire_tuple(tuple_: dict) -> dict:
    """A valid tuple as it goes on the wire: ``lambda_utility: null`` counts as absent."""
    return {k: v for k, v in tuple_.items() if not (k == "lambda_utility" and v is None)}
