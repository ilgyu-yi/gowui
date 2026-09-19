"""Shared helpers for the static frontend tests (SPEC §3.8, §4, §7.5, §8.5).

The page is vanilla JS with no build step, so these tests read its sources with regular
expressions rather than a JS parser. That works because §3.8 and the frontend checks pin a few
structural rules the sources keep, which the tests below both rely on and enforce:

- every frame the page sends goes through one ``send({type: '<literal>', ...})`` in ``app.js``,
  and ``.send(`` on a socket appears exactly once, inside that function;
- every frame the page receives is handled by one ``switch (<name>.type)`` with ``case '<type>':``
  labels;
- the i18n tables in ``i18n.js`` are an ``en: {`` block and a ``ko: {`` block, each line one
  ``'key': 'value',`` (single- or double-quoted value) or a ``//`` comment, closed by ``},`` / ``}``;
- a ``t(...)`` call names its key as a literal, a literal ternary, or a registered dynamic family
  (tests/test_frontend_i18n.py ``DYNAMIC_SITES``).

Count guards in the tests catch a regex that silently collects nothing.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "gowui" / "static"
SPEC = REPO / "SPEC.md"

#: The five scripts of §3.8, in their load order.
SCRIPTS = ("i18n.js", "tuple.js", "profile.js", "board.js", "app.js")


def static_file(relative: str) -> Path:
    return STATIC / relative


def read_static(relative: str) -> str:
    """The text of a page file; a missing file fails the test that needs it (not an error)."""
    path = static_file(relative)
    if not path.is_file():
        pytest.fail(f"gowui/static/{relative} does not exist (SPEC §3.8)")
    return path.read_text(encoding="utf-8")


def read_js(name: str) -> str:
    return read_static(f"js/{name}")


def all_js() -> dict[str, str]:
    """Every §3.8 script by name; fails when one is missing."""
    return {name: read_js(name) for name in SCRIPTS}


def strip_js_comments(text: str) -> str:
    """``text`` with ``/* */`` and ``//`` comments blanked (string contents are kept).

    Good enough for the page's own sources: a ``//`` inside a string literal (a URL) is kept
    because the scan tracks quotes.
    """
    out: list[str] = []
    i, n = 0, len(text)
    quote: str | None = None
    while i < n:
        c = text[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in "'\"`":
            quote = c
            out.append(c)
            i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = n if end < 0 else end + 2
            out.append("".join("\n" if ch == "\n" else " " for ch in text[i:end]))
            i = end
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            out.append(" " * (end - i))
            i = end
            continue
        out.append(c)
        i += 1
    return "".join(out)


# -- node --------------------------------------------------------------------------------------
def node_or_skip() -> str:
    """The ``node`` executable. Without one the test is skipped locally and fails in CI (``CI``
    set), where the frontend checks are required (README "Frontend checks")."""
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on the PATH; the frontend checks need it in CI")
        pytest.skip("node is not on the PATH (the frontend checks need it; required in CI)")
    return node


def run_node(args: list[str], *, stdin: str | None = None, timeout: float = 30) -> \
        subprocess.CompletedProcess:
    node = node_or_skip()
    return subprocess.run([node, *args], input=stdin, capture_output=True, text=True,
                          timeout=timeout, cwd=str(REPO))


# -- i18n tables --------------------------------------------------------------------------------
_KEY = r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*"
_VALUE = r"(?:'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
TABLE_LINE = re.compile(rf"^\s*'(?P<key>{_KEY})'\s*:\s*(?P<value>{_VALUE})\s*,?\s*$")
COMMENT_LINE = re.compile(r"^\s*(?://.*)?$")
TABLE_OPEN = re.compile(r"^\s*(?P<lang>en|ko)\s*:\s*\{\s*$")
TABLE_CLOSE = re.compile(r"^\s*\}\s*,?\s*$")


def _js_string(literal: str) -> str:
    """The value of a simple single- or double-quoted JS string literal."""
    body = literal[1:-1]
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)), body)


def parse_tables(text: str) -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    """The ``en`` and ``ko`` tables as ``{lang: [(key, value), ...]}`` (duplicates kept), and the
    lines inside a table that do not have the strict shape."""
    tables: dict[str, list[tuple[str, str]]] = {}
    bad: list[str] = []
    current: str | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if current is None:
            opened = TABLE_OPEN.match(line)
            if opened:
                current = opened.group("lang")
                tables[current] = []
            continue
        if TABLE_CLOSE.match(line):
            current = None
            continue
        entry = TABLE_LINE.match(line)
        if entry:
            tables[current].append((entry.group("key"), _js_string(entry.group("value"))))
        elif not COMMENT_LINE.match(line):
            bad.append(f"i18n.js:{number}: {line.strip()}")
    return tables, bad


def i18n_tables() -> dict[str, dict[str, str]]:
    tables, _ = parse_tables(read_js("i18n.js"))
    return {lang: dict(entries) for lang, entries in tables.items()}


# -- the page's frames -----------------------------------------------------------------------------
SEND_CALL = re.compile(r"(?<![\w.$])send\s*\(")
SEND_LITERAL = re.compile(r"(?<![\w.$])send\s*\(\s*\{\s*type\s*:\s*'(?P<type>[a-z_]+)'")
SOCKET_SEND = re.compile(r"\.send\s*\(")
SWITCH_TYPE = re.compile(r"switch\s*\(\s*[\w.]+\.type\s*\)")
CASE_LABEL = re.compile(r"case\s+'(?P<type>[a-z_]+)'\s*:")


def send_calls(js: str) -> list[str]:
    """The text after every bare ``send(`` call site (definitions excluded)."""
    code = strip_js_comments(js)
    sites = []
    for match in SEND_CALL.finditer(code):
        before = code[max(0, match.start() - 10):match.start()]
        if re.search(r"function\s*$", before):
            continue
        sites.append(code[match.start():match.start() + 120])
    return sites


def sent_types(js: str) -> list[str]:
    return [m.group("type") for m in SEND_LITERAL.finditer(strip_js_comments(js))]


def switch_block(js: str) -> str:
    """The body of the one ``switch (x.type)`` in ``js`` (empty when there is none)."""
    code = strip_js_comments(js)
    match = SWITCH_TYPE.search(code)
    if not match:
        return ""
    start = code.find("{", match.end())
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return code[start:]


# -- SPEC tables ----------------------------------------------------------------------------------
def spec_section(heading: str) -> str:
    """The text of the SPEC section whose heading line starts with ``heading`` (for example
    ``"### 4.1"``), up to the next heading of the same or a higher level."""
    lines = SPEC.read_text(encoding="utf-8").splitlines()
    level = len(heading.split()[0])
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith(heading):
            inside = True
            continue
        if inside and re.match(rf"^#{{1,{level}}} ", line):
            break
        if inside:
            out.append(line)
    return "\n".join(out)


def spec_table_types(heading: str) -> set[str]:
    """The message types named in the first column of the table under ``heading``."""
    types: set[str] = set()
    for line in spec_section(heading).splitlines():
        if not line.startswith("| `"):
            continue
        first = line.split("|")[1]
        types.update(re.findall(r"`([a-z_]+)`", first))
    return types
