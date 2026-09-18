"""A position: stones and prisoner counts, with captures and suicide (SPEC §1.3)."""

from __future__ import annotations

EMPTY = 0
BLACK = 1
WHITE = 2

COLOR_NAMES = {BLACK: "black", WHITE: "white"}


def opponent(color: int) -> int:
    return WHITE if color == BLACK else BLACK


def color_from_name(name: str) -> int:
    """Accept ``b``/``black`` and ``w``/``white`` in any case."""
    key = name.strip().lower() if isinstance(name, str) else ""
    if key in ("b", "black"):
        return BLACK
    if key in ("w", "white"):
        return WHITE
    raise ValueError(f"unknown color {name!r}")


class IllegalMove(Exception):
    """A move the rules in force refuse."""


class Board:
    """Stones (row-major ``bytearray`` from the top-left) plus prisoners per capturing colour."""

    __slots__ = ("size", "stones", "captures")

    def __init__(self, size: int, stones: bytearray | None = None,
                 captures: dict[int, int] | None = None) -> None:
        self.size = size
        self.stones = bytearray(size * size) if stones is None else stones
        self.captures = {BLACK: 0, WHITE: 0} if captures is None else captures

    def on_board(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def get(self, x: int, y: int) -> int:
        return self.stones[y * self.size + x]

    def set(self, x: int, y: int, color: int) -> None:
        self.stones[y * self.size + x] = color

    def copy(self) -> "Board":
        return Board(self.size, bytearray(self.stones), dict(self.captures))

    def position_key(self) -> bytes:
        """The stones alone, as an immutable key."""
        return bytes(self.stones)

    def _neighbours(self, i: int):
        size = self.size
        x = i % size
        if x > 0:
            yield i - 1
        if x + 1 < size:
            yield i + 1
        if i >= size:
            yield i - size
        if i + size < size * size:
            yield i + size

    def _chain(self, i: int) -> tuple[list[int], bool]:
        """The chain through index ``i`` and whether it has a liberty."""
        stones = self.stones
        color = stones[i]
        seen = {i}
        stack = [i]
        has_liberty = False
        while stack:
            c = stack.pop()
            for n in self._neighbours(c):
                value = stones[n]
                if value == EMPTY:
                    has_liberty = True
                elif value == color and n not in seen:
                    seen.add(n)
                    stack.append(n)
        return list(seen), has_liberty

    def play(self, color: int, x: int, y: int, allow_suicide: bool = False) -> list[tuple[int, int]]:
        """Place a stone, resolve captures, then suicide; return the captured points.

        A refused move leaves the board unchanged. Where suicide is allowed, the player's own
        chain is removed and counted as prisoners for the opponent.
        """
        if not self.on_board(x, y):
            raise IllegalMove(f"({x}, {y}) is off the board")
        i = y * self.size + x
        stones = self.stones
        if stones[i] != EMPTY:
            raise IllegalMove("point is occupied")
        stones[i] = color
        enemy = opponent(color)
        captured: list[int] = []
        for n in self._neighbours(i):
            if stones[n] == enemy:
                chain, has_liberty = self._chain(n)
                if not has_liberty:
                    for c in chain:
                        stones[c] = EMPTY
                    captured.extend(chain)
        if captured:
            self.captures[color] += len(captured)
            return [(c % self.size, c // self.size) for c in captured]
        own, has_liberty = self._chain(i)
        if not has_liberty:
            if not allow_suicide:
                stones[i] = EMPTY
                raise IllegalMove("suicide is not allowed under these rules")
            for c in own:
                stones[c] = EMPTY
            self.captures[enemy] += len(own)
        return []
