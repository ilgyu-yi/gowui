"""Coordinate conversions: GTP vertices (SPEC §0), SGF points (§1.5), handicap points (§1.2).

Internally a point is ``(x, y)`` with ``(0, 0)`` at the top-left, ``x`` to the right and ``y``
downwards. GTP columns are ``A``..``Z`` without ``I`` and rows count from 1 at the bottom; SGF
labels both axes ``a``.. from the top-left.
"""

from __future__ import annotations

import re

GTP_COLUMNS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"
SGF_LETTERS = "abcdefghijklmnopqrstuvwxy"

PASS = "pass"

# One column letter (no I, either case), then 1-2 ASCII digits without a leading zero.
_VERTEX = re.compile(r"([A-HJ-Za-hj-z])([1-9][0-9]?)")


class CoordinateError(ValueError):
    """A coordinate that cannot be parsed or written for the given board."""


def _check_bounds(x: int, y: int, size: int, original: object = None) -> None:
    if not (0 <= x < size and 0 <= y < size):
        label = original if original is not None else f"({x}, {y})"
        raise CoordinateError(f"{label!r} is outside a {size}x{size} board")


def to_gtp(point: tuple[int, int] | None, size: int) -> str:
    """Write a point as a GTP vertex; ``None`` is ``pass``."""
    if point is None:
        return PASS
    x, y = point
    _check_bounds(x, y, size)
    return f"{GTP_COLUMNS[x]}{size - y}"


def from_gtp(vertex: str, size: int) -> tuple[int, int] | None:
    """Read a GTP vertex strictly; ``pass`` is ``None``, ``resign`` and anything else is refused."""
    if not isinstance(vertex, str):
        raise CoordinateError(f"bad vertex {vertex!r}")
    if vertex.lower() == PASS:
        return None
    match = _VERTEX.fullmatch(vertex)
    if match is None:
        raise CoordinateError(f"bad vertex {vertex!r}")
    x = GTP_COLUMNS.index(match.group(1).upper())
    y = size - int(match.group(2))
    _check_bounds(x, y, size, vertex)
    return x, y


def to_sgf(point: tuple[int, int] | None, size: int) -> str:
    """Write a point as SGF coordinates; a pass is the empty value."""
    if point is None:
        return ""
    x, y = point
    _check_bounds(x, y, size)
    return f"{SGF_LETTERS[x]}{SGF_LETTERS[y]}"


def from_sgf(text: str, size: int) -> tuple[int, int] | None:
    """Read an SGF move value: empty (or ``tt`` up to 19x19) is a pass, else a point on the board."""
    if text == "":
        return None
    if text == "tt" and size <= 19:
        return None
    return sgf_point(text, size)


def sgf_point(text: str, size: int) -> tuple[int, int]:
    """Read two SGF letters naming a point on the board (no pass forms)."""
    if not isinstance(text, str) or len(text) != 2:
        raise CoordinateError(f"bad SGF point {text!r}")
    x = SGF_LETTERS.find(text[0])
    y = SGF_LETTERS.find(text[1])
    if x < 0 or y < 0:
        raise CoordinateError(f"bad SGF point {text!r}")
    _check_bounds(x, y, size, text)
    return x, y


def handicap_allowed(size: int, count: int) -> bool:
    """Whether a new game of this size accepts ``count`` handicap stones (SPEC §1.2)."""
    if count in (0, 1):
        return True
    if count < 0 or size < 7:
        return False
    if size == 7 or size % 2 == 0:
        return 2 <= count <= 4
    return 2 <= count <= 9


def handicap_points(size: int, count: int) -> list[tuple[int, int]]:
    """GTP 2 ``fixed_handicap`` points, in GTP's order; 0 or 1 is no handicap."""
    if not handicap_allowed(size, count):
        raise ValueError(f"a {size}x{size} board does not allow a handicap of {count}")
    if count < 2:
        return []
    edge = 2 if size <= 12 else 3
    low, high, mid = edge, size - 1 - edge, size // 2
    corners = [(low, high), (high, low), (low, low), (high, high)]
    if count <= 4:
        return corners[:count]
    sides = [(low, mid), (high, mid)]
    ends = [(mid, high), (mid, low)]
    centre = [(mid, mid)]
    return {
        5: corners + centre,
        6: corners + sides,
        7: corners + sides + centre,
        8: corners + sides + ends,
        9: corners + sides + ends + centre,
    }[count]
