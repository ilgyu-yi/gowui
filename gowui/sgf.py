"""SGF text: a reader for the main line and a writer (SPEC §1.5).

The reader is iterative, so its cost is linear in the text and deep nesting cannot exhaust the
Python stack; variations are checked for syntax and dropped.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Deepest variation nesting the reader accepts (SPEC §1.5).
MAX_DEPTH = 1000
#: Longest main line the reader accepts, in nodes (SPEC §1.5, §7.6).
MAX_NODES = 10_000

_PLAIN = re.compile(r"[^\\\]]*")
_IDENT = re.compile(r"[A-Za-z]+")


class SGFError(ValueError):
    """Text that cannot be read as an SGF game."""


@dataclass
class SGFNode:
    properties: dict[str, list[str]] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        values = self.properties.get(key)
        return values[0] if values else default


def parse(text: str) -> list[SGFNode]:
    """Return the main-line nodes of the first game tree in ``text``."""
    if not isinstance(text, str):
        raise SGFError("SGF must be text")
    reader = _Reader(text)
    return reader.game_tree()


def dump(nodes: list[SGFNode]) -> str:
    """Write nodes as one game tree on one line."""
    return "(" + "".join(_dump_node(node) for node in nodes) + ")\n"


def _dump_node(node: SGFNode) -> str:
    parts = [";"]
    for key, values in node.properties.items():
        parts.append(key + "".join(f"[{escape(v)}]" for v in values))
    return "".join(parts)


def escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("]", "\\]")


class _Reader:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def _skip_space(self) -> str:
        text, pos = self.text, self.pos
        while pos < len(text) and text[pos].isspace():
            pos += 1
        self.pos = pos
        return text[pos] if pos < len(text) else ""

    def game_tree(self) -> list[SGFNode]:
        if self._skip_space() != "(":
            raise SGFError("SGF must start with '('")
        self.pos += 1
        nodes: list[SGFNode] = []
        # One frame per open '(' : [on the main line, subtrees seen so far].
        stack: list[list] = [[True, 0]]
        while stack:
            char = self._skip_space()
            frame = stack[-1]
            if char == ";":
                if frame[1]:
                    raise SGFError(f"node after a variation at offset {self.pos}")
                if frame[0] and len(nodes) >= MAX_NODES:
                    raise SGFError(f"main line longer than {MAX_NODES} nodes")
                self.pos += 1
                node = self._node()
                if frame[0]:
                    nodes.append(node)
            elif char == "(":
                if len(stack) >= MAX_DEPTH:
                    raise SGFError(f"variations nested more than {MAX_DEPTH} levels deep")
                self.pos += 1
                main = frame[0] and frame[1] == 0
                frame[1] += 1
                stack.append([main, 0])
            elif char == ")":
                self.pos += 1
                stack.pop()
            elif char == "":
                raise SGFError("unexpected end of SGF: missing ')'")
            else:
                raise SGFError(f"unexpected character {char!r} at offset {self.pos}")
        if not nodes:
            raise SGFError("SGF contains no nodes")
        return nodes

    def _node(self) -> SGFNode:
        node = SGFNode()
        while True:
            self._skip_space()
            match = _IDENT.match(self.text, self.pos)
            if match is None:
                return node
            self.pos = match.end()
            ident = match.group()
            key = "".join(c for c in ident if c.isupper()) or ident.upper()
            values: list[str] = []
            while self._skip_space() == "[":
                self.pos += 1
                values.append(self._value())
            if not values:
                raise SGFError(f"property {ident} has no value")
            node.properties.setdefault(key, []).extend(values)

    def _value(self) -> str:
        text = self.text
        out: list[str] = []
        while True:
            match = _PLAIN.match(text, self.pos)
            out.append(match.group())
            pos = match.end()
            if pos >= len(text):
                raise SGFError("unexpected end of SGF inside a property value")
            if text[pos] == "]":
                self.pos = pos + 1
                return "".join(out)
            # A backslash: escapes the next character; before a line break it is a soft break.
            pos += 1
            if pos >= len(text):
                raise SGFError("unexpected end of SGF inside a property value")
            nxt = text[pos]
            if nxt in "\r\n":
                pos += 1
                if pos < len(text) and text[pos] in "\r\n" and text[pos] != nxt:
                    pos += 1
            else:
                out.append(nxt)
                pos += 1
            self.pos = pos
