"""The shared analysis shape (SPEC §2.2), positions, and the sanitising of engine numbers.

Every protocol reports into :class:`Analysis`, whose ``to_dict`` is the browser's ``analysis``
message. Engine output is untrusted: numbers pass through :func:`finite` (non-finite or malformed
becomes ``None``; inside per-point arrays, ``0``) and vertices through :func:`board_vertex`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

from .. import coords


# -- sanitising -----------------------------------------------------------------------
def finite(value: Any) -> float | None:
    """``value`` as a finite float, or ``None`` when it is non-finite, malformed or a boolean."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return None
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def finite_int(value: Any) -> int | None:
    """``value`` as an int (a finite float is truncated), or ``None``."""
    number = finite(value)
    return None if number is None else int(number)


def point_values(values: Any, flip: bool = False, length: int | None = None) -> list[float]:
    """A per-point array (ownership, policy): anything not a finite number becomes ``0``.

    An array that is not ``length`` long (when given) is dropped whole: ``[]``.
    """
    if not isinstance(values, (list, tuple)):
        return []
    if length is not None and len(values) != length:
        return []
    out: list[float] = []
    for value in values:
        number = finite(value)
        if number is None or number == 0:
            out.append(0.0)
        else:
            out.append(-number if flip else number)
    return out


#: A reported turn number is clamped to this range (§2.2).
MAX_TURN = 10000


def clamp_turn(value: int) -> int:
    return max(0, min(MAX_TURN, value))


def board_vertex(move: Any, size: int) -> str | None:
    """A GTP vertex on a ``size`` board (canonical upper case) or ``pass``; anything else ``None``."""
    if not isinstance(move, str):
        return None
    try:
        return coords.to_gtp(coords.from_gtp(move, size), size)
    except ValueError:
        return None


def board_vertices(moves: Iterable[Any], size: int) -> list[str]:
    """A principal variation with every vertex that is not on the board (and not pass) dropped."""
    out = []
    for move in moves:
        vertex = board_vertex(move, size)
        if vertex is not None:
            out.append(vertex)
    return out


def flip_rate(value: Any, black_to_play: bool) -> float | None:
    number = finite(value)
    if number is None:
        return None
    return number if black_to_play else 1.0 - number


def flip_score(value: Any, black_to_play: bool) -> float | None:
    number = finite(value)
    if number is None:
        return None
    return number if black_to_play else (-number or 0.0)


# -- positions ----------------------------------------------------------------------------
@dataclass
class Position:
    """Everything an engine needs to rebuild the position: setup, moves and who starts."""

    size: int
    komi: float
    rules: str
    initial_stones: list[list[str]]
    moves: list[list[str]]
    #: Who moves first from the setup position -- "B" or "W".
    first_player: str = "B"

    def black_to_play(self) -> bool:
        if self.moves:
            return not str(self.moves[-1][0]).upper().startswith("B")
        return str(self.first_player).upper().startswith("B")

    @property
    def to_play(self) -> str:
        return "B" if self.black_to_play() else "W"


# -- the shared analysis shape ----------------------------------------------------------------
@dataclass
class MoveInfo:
    """One candidate move, from Black's point of view."""

    move: str
    visits: int | None = 0
    winrate: float | None = None
    score_lead: float | None = None
    score_mean: float | None = None
    score_stdev: float | None = None
    prior: float | None = None
    lcb: float | None = None
    utility: float | None = None
    utility_lcb: float | None = None
    order: int | None = 0
    pv: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "move": self.move,
            "visits": self.visits,
            "winrate": self.winrate,
            "scoreLead": self.score_lead,
            "scoreMean": self.score_mean,
            "scoreStdev": self.score_stdev,
            "prior": self.prior,
            "lcb": self.lcb,
            "utility": self.utility,
            "utilityLcb": self.utility_lcb,
            "order": self.order,
            "pv": list(self.pv),
        }


@dataclass
class RootInfo:
    """The engine's evaluation of the position itself, from Black's point of view."""

    visits: int | None = 0
    winrate: float | None = None
    score_lead: float | None = None
    score_mean: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "visits": self.visits,
            "winrate": self.winrate,
            "scoreLead": self.score_lead,
            "scoreMean": self.score_mean,
        }


@dataclass
class Analysis:
    """A snapshot of the engine's search; every number is Black's view (SPEC §0)."""

    move_infos: list[MoveInfo] = field(default_factory=list)
    root: RootInfo = field(default_factory=RootInfo)
    ownership: list[float] = field(default_factory=list)
    policy: list[float] = field(default_factory=list)
    turn: int = 0
    complete: bool = False
    source: str = ""
    #: "B" or "W" -- the player the engine searched for.
    current_player: str = ""
    #: handol-mux only (SPEC §2.5).
    compare: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "moveInfos": [m.to_dict() for m in self.move_infos],
            "rootInfo": self.root.to_dict(),
            "ownership": list(self.ownership),
            "policy": list(self.policy),
            "turn": self.turn,
            "complete": self.complete,
            "source": self.source,
            "currentPlayer": self.current_player,
            "compare": self.compare,
        }


def sort_by_order(infos: list[MoveInfo]) -> None:
    """Order candidates by the engine's ranking; an unreadable rank goes last."""
    infos.sort(key=lambda m: (m.order is None, m.order if m.order is not None else 0))
