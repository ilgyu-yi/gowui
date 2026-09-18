"""The game model (SPEC §1.2-§1.5): setup stones, one line of play and a cursor.

Every ply is stored once as an immutable ``bytes`` snapshot of the stones (which is also its
positional key) with the prisoner counts after it. Two indexes map a positional key, and a
(key, player to move) pair, to the first ply where it occurred; a position has occurred at the
cursor iff that first ply is at or before the cursor. A legality check therefore costs time
proportional to the board area, whatever the game's length.
"""

from __future__ import annotations

import importlib.metadata
import math
import re
from dataclasses import dataclass
from typing import Any

from . import coords
from .board import BLACK, EMPTY, WHITE, Board, IllegalMove, opponent
from .coords import CoordinateError
from .rules import (DEFAULT_RULES, HANDICAP_KOMI, POSITIONAL_SUPERKO, SIMPLE_KO,
                    SITUATIONAL_SUPERKO, RuleSet, get_rules)
from .sgf import SGFError, SGFNode, dump as sgf_dump, parse as sgf_parse

RESULT_UNKNOWN = ""

_RESIGNATION = re.compile(r"[BW]\+R(esign)?")
_SZ = re.compile(r"([0-9]+)(?::([0-9]+))?")
_HA = re.compile(r"[+-]?[0-9]+")
_KM = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

_RULE_ALIASES = {
    "jp": "japanese",
    "cn": "chinese",
    "nz": "new-zealand",
    "tromptaylor": "tromp-taylor",
}


def _version() -> str:
    try:
        return importlib.metadata.version("gowui")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _color_name(color: int) -> str:
    return "black" if color == BLACK else "white"


@dataclass(frozen=True)
class Move:
    """One played move; ``point`` is ``None`` for a pass, ``captures`` the stones it took."""

    color: int
    point: tuple[int, int] | None
    captures: int = 0
    comment: str = ""


class Game:
    """Board size, rules, setup stones and the line of play, with undo and navigation."""

    def __init__(self, size: int = 19, komi: float | None = None, rules: str = DEFAULT_RULES,
                 handicap: int = 0) -> None:
        if isinstance(size, bool) or not isinstance(size, int) or not 2 <= size <= 25:
            raise ValueError("board size must be between 2 and 25")
        rule_set = get_rules(rules)
        if isinstance(handicap, bool) or not isinstance(handicap, int):
            raise ValueError("handicap must be an integer")
        if komi is not None and not math.isfinite(float(komi)):
            raise ValueError("komi must be a finite number")
        points = coords.handicap_points(size, handicap)
        count = len(points)
        self._setup(size, rule_set, komi, count, [(BLACK, x, y) for x, y in points],
                    WHITE if count else BLACK)

    def _setup(self, size: int, rule_set: RuleSet, komi: float | None, handicap: int,
               setup_stones: list[tuple[int, int, int]], first_player: int) -> None:
        self.size = size
        self.rules = rule_set
        self.handicap = handicap
        if komi is not None:
            self.komi = float(komi)
        else:
            self.komi = HANDICAP_KOMI if handicap else rule_set.default_komi
        self.result: str = RESULT_UNKNOWN
        self.player_names: dict[int, str] = {BLACK: "Black", WHITE: "White"}
        self.setup_stones = setup_stones
        self.first_player = first_player
        self.moves: list[Move] = []
        self.cursor = 0
        start = bytearray(size * size)
        for color, x, y in setup_stones:
            start[y * size + x] = color
        key = bytes(start)
        self._snapshots: list[bytes] = [key]
        self._prisoners: list[tuple[int, int]] = [(0, 0)]
        self._first_seen: dict[bytes, int] = {key: 0}
        self._first_seen_to_play: dict[tuple[bytes, int], int] = {(key, first_player): 0}

    # -- state --------------------------------------------------------------
    @property
    def board(self) -> Board:
        """A fresh copy of the position at the cursor."""
        black, white = self._prisoners[self.cursor]
        return Board(self.size, bytearray(self._snapshots[self.cursor]), {BLACK: black, WHITE: white})

    @property
    def move_count(self) -> int:
        return len(self.moves)

    def _to_play_at(self, ply: int) -> int:
        return opponent(self.moves[ply - 1].color) if ply else self.first_player

    @property
    def to_play(self) -> int:
        return self._to_play_at(self.cursor)

    @property
    def last_move(self) -> Move | None:
        return self.moves[self.cursor - 1] if self.cursor else None

    def is_game_over(self) -> bool:
        """Two passes just before the cursor, or a resignation with the cursor at the last move."""
        c = self.cursor
        if c >= 2 and self.moves[c - 1].point is None and self.moves[c - 2].point is None:
            return True
        return c == len(self.moves) and _RESIGNATION.fullmatch(self.result) is not None

    # -- legality -------------------------------------------------------------
    def _attempt(self, color: int, point: tuple[int, int] | None) -> tuple[str | None, Board | None, int]:
        """Try the move at the cursor: (reason it is illegal or None, resulting board, captures)."""
        if color not in (BLACK, WHITE):
            return "unknown colour", None, 0
        board = self.board
        if point is None:
            return None, board, 0
        try:
            x, y = point
            on_board = board.on_board(x, y)
        except (TypeError, ValueError):
            return "not a point", None, 0
        if not on_board:
            return "off the board", None, 0
        if board.get(x, y) != EMPTY:
            return "point is occupied", None, 0
        try:
            captured = board.play(color, x, y, allow_suicide=self.rules.suicide)
        except IllegalMove as exc:
            return str(exc), None, 0
        key = bytes(board.stones)
        ko = self.rules.ko
        if ko == SIMPLE_KO:
            if self.cursor and key == self._snapshots[self.cursor - 1]:
                return "ko: this recreates the position before the opponent's move", None, 0
        elif ko == POSITIONAL_SUPERKO:
            if self._first_seen.get(key, self.cursor + 1) <= self.cursor:
                return "superko: this position has already occurred", None, 0
        elif ko == SITUATIONAL_SUPERKO:
            if self._first_seen_to_play.get((key, opponent(color)), self.cursor + 1) <= self.cursor:
                return ("superko: this position with the same player to move "
                        "has already occurred"), None, 0
        return None, board, len(captured)

    def legal_error(self, color: int, point: tuple[int, int] | None) -> str | None:
        """Why the move is illegal at the cursor, or ``None`` when it is legal."""
        return self._attempt(color, point)[0]

    def legal_moves(self, color: int | None = None) -> list[tuple[int, int]]:
        color = self.to_play if color is None else color
        return [(x, y) for y in range(self.size) for x in range(self.size)
                if self.legal_error(color, (x, y)) is None]

    # -- the line of play -------------------------------------------------------
    def _truncate(self, ply: int) -> None:
        """Drop every ply after ``ply``, and the index entries first seen there."""
        for i in range(len(self._snapshots) - 1, ply, -1):
            key = self._snapshots[i]
            if self._first_seen.get(key) == i:
                del self._first_seen[key]
            situation = (key, opponent(self.moves[i - 1].color))
            if self._first_seen_to_play.get(situation) == i:
                del self._first_seen_to_play[situation]
        del self._snapshots[ply + 1:]
        del self._prisoners[ply + 1:]
        del self.moves[ply:]

    def play(self, color: int, point: tuple[int, int] | None, comment: str = "") -> Move:
        """Play at the cursor, discarding any later moves; a move clears the result."""
        error, board, captured = self._attempt(color, point)
        if error:
            raise IllegalMove(error)
        self._truncate(self.cursor)
        move = Move(color, point, captured, comment)
        key = bytes(board.stones)
        ply = len(self._snapshots)
        self.moves.append(move)
        self._snapshots.append(key)
        self._prisoners.append((board.captures[BLACK], board.captures[WHITE]))
        self._first_seen.setdefault(key, ply)
        self._first_seen_to_play.setdefault((key, opponent(color)), ply)
        self.cursor = ply
        self.result = RESULT_UNKNOWN
        return move

    def undo(self) -> Move | None:
        """Remove the move before the cursor and everything after it; clears the result."""
        self.result = RESULT_UNKNOWN
        if not self.cursor:
            return None
        move = self.moves[self.cursor - 1]
        self.cursor -= 1
        self._truncate(self.cursor)
        return move

    def navigate(self, index: int) -> int:
        self.cursor = max(0, min(int(index), len(self.moves)))
        return self.cursor

    def resign(self, color: int) -> None:
        """``color`` resigns: ``W+R`` when Black resigns, ``B+R`` when White does."""
        if color not in (BLACK, WHITE):
            raise ValueError(f"unknown colour {color!r}")
        self.result = "W+R" if color == BLACK else "B+R"

    # -- browser-facing state ---------------------------------------------------
    def move_numbers(self) -> dict[str, int]:
        """Vertex -> move number for every stone on the board at the cursor that a move placed."""
        stones = self._snapshots[self.cursor]
        latest: dict[tuple[int, int], int] = {}
        for n, move in enumerate(self.moves[: self.cursor], start=1):
            if move.point is not None:
                latest[move.point] = n
        return {coords.to_gtp((x, y), self.size): n for (x, y), n in latest.items()
                if stones[y * self.size + x] != EMPTY}

    def to_dict(self) -> dict[str, Any]:
        black, white = self._prisoners[self.cursor]
        last = self.last_move
        return {
            "size": self.size,
            "komi": self.komi,
            "rules": self.rules.name,
            "handicap": self.handicap,
            "stones": list(self._snapshots[self.cursor]),
            "captures": {"black": black, "white": white},
            "toPlay": _color_name(self.to_play),
            "cursor": self.cursor,
            "moveCount": len(self.moves),
            "gameOver": self.is_game_over(),
            "result": self.result,
            "players": {"black": self.player_names[BLACK], "white": self.player_names[WHITE]},
            "lastMove": coords.to_gtp(last.point, self.size) if last else None,
            "setupStones": [{"color": _color_name(c), "vertex": coords.to_gtp((x, y), self.size)}
                            for c, x, y in self.setup_stones],
            "moves": [{"n": n, "color": _color_name(m.color),
                       "vertex": coords.to_gtp(m.point, self.size),
                       "captures": m.captures, "comment": m.comment}
                      for n, m in enumerate(self.moves, start=1)],
            "moveNumbers": self.move_numbers(),
        }

    # -- SGF --------------------------------------------------------------------
    def to_sgf(self) -> str:
        root = SGFNode()
        props = root.properties
        props.update({
            "GM": ["1"], "FF": ["4"], "CA": ["UTF-8"], "AP": [f"gowui:{_version()}"],
            "SZ": [str(self.size)], "KM": [repr(self.komi)], "RU": [self.rules.katago],
            "PB": [self.player_names[BLACK]], "PW": [self.player_names[WHITE]],
        })
        if self.handicap:
            props["HA"] = [str(self.handicap)]
        if self.first_player != (WHITE if self.handicap else BLACK):
            props["PL"] = ["B" if self.first_player == BLACK else "W"]
        if self.result:
            props["RE"] = [self.result]
        for key, color in (("AB", BLACK), ("AW", WHITE)):
            points = [coords.to_sgf((x, y), self.size) for c, x, y in self.setup_stones if c == color]
            if points:
                props[key] = points
        nodes = [root]
        for move in self.moves:
            node = SGFNode()
            node.properties["B" if move.color == BLACK else "W"] = [coords.to_sgf(move.point, self.size)]
            if move.comment:
                node.properties["C"] = [move.comment]
            nodes.append(node)
        return sgf_dump(nodes)

    @classmethod
    def from_sgf(cls, text: str) -> "Game":
        """Read the main line of an SGF game; unreadable text raises :class:`SGFError`."""
        nodes = sgf_parse(text)
        root = nodes[0]
        size = _sgf_size(root)
        rule_set = get_rules(_sgf_rules(root.get("RU")))
        komi = _sgf_komi(root)
        ha = _sgf_handicap(root)
        setup = _sgf_setup(root, size)
        handicap = ha if setup and ha >= 2 else 0
        first = WHITE if handicap else BLACK
        player = root.get("PL").strip().lower()
        if player in ("b", "black"):
            first = BLACK
        elif player in ("w", "white"):
            first = WHITE

        game = cls.__new__(cls)
        game._setup(size, rule_set, komi, handicap, setup, first)
        game.player_names[BLACK] = root.get("PB") or "Black"
        game.player_names[WHITE] = root.get("PW") or "White"

        for node in nodes[1:]:
            props = node.properties
            if "AB" in props or "AW" in props or "AE" in props:
                break
            moves = [(key, color) for key, color in (("B", BLACK), ("W", WHITE)) if key in props]
            try:
                for key, color in moves:
                    game.play(color, coords.from_sgf(node.get(key), size), node.get("C"))
            except (IllegalMove, CoordinateError):
                break
        game.result = root.get("RE")
        return game


def _sgf_size(root: SGFNode) -> int:
    if "SZ" not in root.properties:
        return 19
    match = _SZ.fullmatch(root.get("SZ"))
    if match is None or (match.group(2) is not None and match.group(2) != match.group(1)):
        raise SGFError(f"unsupported SZ {root.get('SZ')!r}")
    size = int(match.group(1))
    if not 2 <= size <= 25:
        raise SGFError(f"SZ {size} is outside 2-25")
    return size


def _sgf_rules(value: str) -> str:
    key = value.strip().lower().replace("_", "-").replace(" ", "-")
    key = _RULE_ALIASES.get(key, key)
    try:
        return get_rules(key).name
    except ValueError:
        return DEFAULT_RULES


def _sgf_komi(root: SGFNode) -> float | None:
    value = root.get("KM").strip()
    if not value:
        return None
    if _KM.fullmatch(value) is None or not math.isfinite(float(value)):
        raise SGFError(f"KM {value!r} is not a finite number")
    return float(value)


def _sgf_handicap(root: SGFNode) -> int:
    if "HA" not in root.properties:
        return 0
    value = root.get("HA").strip()
    if _HA.fullmatch(value) is None:
        raise SGFError(f"HA {value!r} is not a number")
    return int(value)


def _sgf_setup(root: SGFNode, size: int) -> list[tuple[int, int, int]]:
    setup: list[tuple[int, int, int]] = []
    owner: dict[tuple[int, int], int] = {}
    for key, color in (("AB", BLACK), ("AW", WHITE)):
        for value in root.properties.get(key, []):
            for point in _sgf_points(value, size):
                seen = owner.get(point)
                if seen == color:
                    continue
                if seen is not None:
                    raise SGFError(f"{coords.to_sgf(point, size)} is in both AB and AW")
                owner[point] = color
                setup.append((color, point[0], point[1]))
    return setup


def _sgf_points(value: str, size: int) -> list[tuple[int, int]]:
    """One point, or an FF[4] compressed rectangle ``aa:cc``."""
    try:
        if ":" not in value:
            return [coords.sgf_point(value, size)]
        first, _, second = value.partition(":")
        (x1, y1), (x2, y2) = coords.sgf_point(first, size), coords.sgf_point(second, size)
    except CoordinateError as exc:
        raise SGFError(f"bad setup point {value!r}: {exc}") from None
    return [(x, y) for y in range(min(y1, y2), max(y1, y2) + 1)
            for x in range(min(x1, x2), max(x1, x2) + 1)]
