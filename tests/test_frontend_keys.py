"""The page's keyboard table (SPEC §3.7 "Four surfaces, one table"; §3.8 "Help panel"; issue #64).

§3.7 puts every key the page binds into **one table** in ``app.js``, which the document set's
dispatch reads and the help panel renders. §3.8 "Help panel" makes the panel's key list "derived,
not authored", so "a key the page binds and the list omits therefore cannot arise; what can is a
key bound **outside** the table, and that is what a test has to look for".

That is this file. Two sides, and **neither of them authors a key literal**:

* the *census* — every keyboard read in ``gowui/static/js/``, extracted from the shipped source;
* the *table* — every key the shipped source declares, extracted from the same file.

They are compared against each other, which is the generated-output comparison the shell's SPEC
§1.11 L3 permits, and is why no key name is typed into this file. The keys §3.7 names in prose are
pinned by behaviour in tests/browser/test_keyboard.py, not by grepping that prose.

**Two shapes the census has to get right.**

* ``app.js`` compares ``event.code`` to ``4401`` / ``4403`` / ``4429`` on a WebSocket **close**
  event (§3.8 "Connection"). Those are not keyboard reads. They are excluded by their *shape* — a
  comparison against a number — and never by line number, so moving them does not blind the
  census. ``test_the_census_reads_a_close_code_as_a_close_code`` pins that on an authored snippet.
* A census that matches nothing passes every subset check in silence. So the floor:
  ``test_the_census_finds_a_read_for_every_keyboard_handler`` counts the handlers in the same
  source and demands at least that many reads, and
  ``test_the_census_reports_a_key_bound_outside_the_table`` proves on an authored snippet that the
  comparison can fail at all.

**What the census cannot see.** It is anchored on an event-shaped name (``event``, ``evt``, ``ev``,
``e``), because ``tuple.js`` is full of ``field.key`` and a bare ``.key`` would drown the census in
them. ``test_every_keyboard_handler_takes_an_event_shaped_parameter`` keeps the anchor honest for
the handlers themselves; a *helper* the handler delegates to under some other parameter name would
still escape, which is a limit of reading the source with regular expressions and not a licence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pytest

from frontend_helpers import SCRIPTS, all_js, read_js, strip_js_comments

# -- the census ----------------------------------------------------------------------------------
#: A keyboard read: a ``key`` / ``code`` / ``keyCode`` off something event-shaped. The anchor is
#: what keeps ``field.key`` (tuple.js) out; the handlers' own parameter names are pinned below.
EVENT = r"(?:event|evt|ev|e)"
READ = re.compile(rf"\b{EVENT}\.(?:keyCode|key|code)\b")
#: What the read is compared against, when it is compared at all.
RHS = re.compile(r"\s*(?:===|!==|==|!=)\s*"
                 r"(?P<value>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"|-?\d+(?:\.\d+)?)")
NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")

#: Every keyboard handler in the source: a ``key*`` listener or an ``onkey*`` assignment, with the
#: name its function gives the event.
HANDLER = re.compile(r"addEventListener\s*\(\s*'key(?:down|up|press)'\s*,\s*function\s*\(\s*(\w+)"
                     r"|onkey(?:down|up|press)\s*=\s*function\s*\(\s*(\w+)")


def unquote(literal: str) -> str:
    """The value of a simple quoted JS string literal."""
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t"}.get(m.group(1), m.group(1)),
                  literal[1:-1])


@dataclass(frozen=True)
class Read:
    """One keyboard read in the shipped source."""

    script: str
    line: int
    at: int             # offset in the comment-stripped source
    source: str         # the line it sits on, trimmed
    literal: str | None  # the key it is compared against, when it is compared to a string
    number: bool        # compared against a number: a close code, not a key


def census(sources: dict[str, str] | None = None) -> list[Read]:
    """Every keyboard read in ``sources`` (the five scripts by default)."""
    found: list[Read] = []
    for name, text in (sources if sources is not None else all_js()).items():
        code = strip_js_comments(text)
        for match in READ.finditer(code):
            compared = RHS.match(code, match.end())
            value = compared.group("value") if compared else None
            line = code.count("\n", 0, match.start()) + 1
            found.append(Read(name, line, match.start(),
                              code.splitlines()[line - 1].strip(),
                              None if value is None or NUMBER.match(value) else unquote(value),
                              value is not None and NUMBER.match(value) is not None))
    return found


def keyboard_reads(sources: dict[str, str] | None = None) -> list[Read]:
    """The census with the close-code comparisons dropped — by their shape, not their place."""
    return [read for read in census(sources) if not read.number]


def handlers(sources: dict[str, str] | None = None) -> list[tuple[str, str]]:
    """``(script, event parameter name)`` of every keyboard handler in the source."""
    out = []
    for name, text in (sources if sources is not None else all_js()).items():
        for listener, assigned in HANDLER.findall(strip_js_comments(text)):
            out.append((name, listener or assigned))
    return out


# -- the table -----------------------------------------------------------------------------------
#: The table's rows, wherever ``app.js`` assembles them: ``press:`` is the table's own property and
#: is written nowhere else, so the keys are collected without depending on how it is put together.
PRESS = re.compile(r"\bpress\s*:\s*\[(?P<keys>[^\]]*)\]")
LABEL = re.compile(r"\blabel\s*:\s*'(?P<key>[^']+)'")
SCOPE = re.compile(r"\bscope\s*:\s*'(?P<key>[^']+)'")
STRING = re.compile(r"'(?:[^'\\]|\\.)*'")
EXPORT = re.compile(r"\bglobal\.keyHelp\s*=")


def app_code() -> str:
    return strip_js_comments(read_js("app.js"))


def table_presses() -> list[list[str]]:
    """The key list of every row of the table, in source order."""
    return [[unquote(literal) for literal in STRING.findall(match.group("keys"))]
            for match in PRESS.finditer(app_code())]


def table_keys() -> list[str]:
    return [key for row in table_presses() for key in row]


def table_labels() -> list[str]:
    return [m.group("key") for m in LABEL.finditer(app_code())]


def table_scopes() -> list[str]:
    return [m.group("key") for m in SCOPE.finditer(app_code())]


def block_at(code: str, start: int) -> tuple[int, int]:
    """The span of the ``{...}`` block whose opening brace follows ``start``."""
    opening = code.index("{", start)
    depth = 0
    for i in range(opening, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return opening, i + 1
    return opening, len(code)


def handler_bodies(code: str) -> list[tuple[int, int]]:
    return [block_at(code, match.end()) for match in HANDLER.finditer(code)]


def dispatch_sites() -> list[Read]:
    """The keyboard reads that name no key of their own: the table lookups (§3.7)."""
    return [read for read in keyboard_reads() if read.literal is None]


# -- every key the page reads is one the table declares -------------------------------------------
def test_every_key_the_source_reads_is_declared_in_the_table():
    """§3.8 "Help panel": a key bound outside the table is the one failure the panel's derived key
    list cannot catch itself, "and that is what a test has to look for"."""
    declared = set(table_keys())
    outside = sorted({f"{r.script}:{r.line} {r.literal!r} — {r.source}"
                      for r in keyboard_reads() if r.literal is not None
                      and r.literal not in declared})
    assert outside == [], (
        f"{len(outside)} keyboard reads name a key the table does not declare, so the help panel "
        f"cannot list them (§3.7, §3.8 'Help panel'): {outside}")


def test_the_table_declares_the_keys_no_literal_read_serves():
    """§3.7: the document set is dispatched *from* the table, so its ten keys appear in no
    ``=== '<key>'`` anywhere. The table therefore has to declare more than the source reads
    literally — a table that only wrote down the other surfaces' keys would satisfy every subset
    check above and leave the §3.7 set undeclared and unrenderable."""
    read = {r.literal for r in keyboard_reads() if r.literal is not None}
    dispatched = sorted(set(table_keys()) - read)
    assert dispatched, (
        f"the table declares {sorted(set(table_keys()))} and the source reads {sorted(read)} "
        "literally: no key is left for the dispatch to serve, so the document set of §3.7 is "
        "not in the table")


def test_exactly_one_keyboard_read_names_no_key_of_its_own():
    """§3.7: "The dispatch of the document set reads that table" — one lookup, inside a keyboard
    handler. A second uncompared read would be a second dispatch, and a key bound by neither a
    literal nor the table."""
    sites = dispatch_sites()
    code = app_code()
    inside = [read for read in sites
              if read.script == "app.js"
              and any(start <= read.at < end for start, end in handler_bodies(code))]
    assert (len(sites), len(inside)) == (1, 1), (
        "the source has "
        f"{[f'{r.script}:{r.line} {r.source}' for r in sites]} reading a key without naming one; "
        "§3.7 wants exactly one, inside a keyboard handler")


def test_the_dispatch_site_binds_no_key_of_its_own():
    """Issue #64 AC: "one definition, not two". The handler that looks a key up in the table must
    not also carry a map of its own — which is what ``app.js`` does today, rebuilding
    ``var handlers = {ArrowLeft: ..., ...}`` inside the callback on **every keypress**, where no
    second consumer can read it.

    Two readings, because either alone has a hole. A declared object inside the handler is the
    shape of the map being rebuilt there, and it is what makes this red today; the table's own
    keys appearing as properties of that handler is what stays meaningful once the table exists,
    and it names no key this file authored."""
    code = app_code()
    sites = dispatch_sites()
    bodies = [(start, end) for start, end in handler_bodies(code)
              if any(start <= read.at < end for read in sites)]
    body = "".join(code[start:end] for start, end in bodies)
    maps = re.findall(r"var\s+(\w+)\s*=\s*\{", body)
    bound = sorted({key for key in set(table_keys())
                    if re.search(r"[{,\s]\s*(?:%s|'%s'|\"%s\")\s*:"
                                 % (re.escape(key), re.escape(key), re.escape(key)), body)})
    assert (maps, bound) == ([], []), (
        f"the dispatching handler declares {maps} and binds {bound} itself as well as reading "
        "the table, so the key map has two definitions (issue #64 AC 3)")


def test_app_js_exports_the_key_table():
    """§3.8 "Help panel": the list is "rendered from that table", so a second consumer has to be
    able to read it. ``app.js`` exports nothing today; ``profile.js`` is the pattern —
    ``(function (global) { ... global.profileHelp = ...; })(window)``."""
    source = read_js("app.js")
    code = strip_js_comments(source)
    assert (EXPORT.search(code) is not None,
            re.search(r"\(function\s*\(\s*global\s*\)", code) is not None,
            re.search(r"\}\)\(\s*window\s*\)\s*;?\s*$", code.strip()) is not None) == \
        (True, True, True), \
        "app.js does not take a global and export window.keyHelp the way profile.js does"


# -- the floor under the census --------------------------------------------------------------------
def test_the_census_finds_a_read_for_every_keyboard_handler():
    """Count guard, not a feature: every keyboard handler reads at least one key, so a census that
    has stopped matching — the failure this repo has shipped more than once — cannot leave the
    subset checks above green by matching nothing."""
    found, bound = keyboard_reads(), handlers()
    assert len(found) >= len(bound) > 0, (
        f"the census found {len(found)} keyboard reads for {len(bound)} keyboard handlers "
        f"{bound}: it is matching less than the page binds")


def test_every_keyboard_handler_takes_an_event_shaped_parameter():
    """The census is anchored on the event's name, so a handler that called its parameter
    something else would be invisible to it. That is a failure of this file, and it is caught
    here rather than by the reads going quietly missing."""
    unreachable = [(script, name) for script, name in handlers()
                   if not re.fullmatch(EVENT, name)]
    assert unreachable == [], (
        f"{unreachable} name their event something the census does not anchor on ({EVENT}), so "
        "every key they read is outside it")


def test_the_census_reads_a_close_code_as_a_close_code():
    """``app.js`` compares ``event.code`` to ``4401`` / ``4403`` / ``4429`` on a socket **close**
    (§3.8 "Connection"), which is not a keyboard read. It is excluded by its shape — compared
    against a number — so moving those lines cannot blind the census.

    Run on an authored snippet: the shipped file would prove only that the three lines it has
    today are handled."""
    snippet = {"fake.js": "if (event.code === 4401) { go(); }\n"
                          "if (event.key === 'Escape') { stop(); }\n"}
    assert [(r.number, r.literal) for r in census(snippet)] == [(True, None), (False, "Escape")]


def test_the_census_reports_a_key_bound_outside_the_table():
    """The comparison can fail: a read of a key no ``press:`` declares is reported. Without this,
    every assertion above would be green on a census that matched nothing."""
    snippet = {"fake.js": "if (event.key === 'F13') { launch(); }\n"}
    assert [r.literal for r in keyboard_reads(snippet) if r.literal not in set(table_keys())] == \
        ["F13"]


# -- the table's own strings (§3.8 "Language") ----------------------------------------------------
def test_every_row_of_the_table_carries_one_label():
    """Structure guard: the panel renders one named row per key group, so a row without a label
    would render a blank line — and the i18n checks below would have one fewer key to look for."""
    assert (len(table_labels()), len(table_scopes()) > 0) == (len(table_presses()), True), (
        f"{len(table_presses())} key groups carry {len(table_labels())} labels across "
        f"{len(table_scopes())} scopes")


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_label_and_scope_of_the_table_is_in_the_table_of_strings(lang):
    """§3.8 "Language" and issue #64 AC 5: "Every new string is in both ``en`` and ``ko``". The
    key list's own strings are properties of an object, which the ``used_keys()`` reader of
    tests/test_frontend_i18n.py cannot see, so they are collected here instead.

    ``i18n.t`` falls back to the key itself, so a label missing from a table is not an error on
    the page — it is a row reading ``help.key.prev``. This is where that is caught."""
    from test_frontend_i18n import table

    keys = set(table_labels()) | set(table_scopes())
    assert (sorted(keys - set(table(lang))), bool(keys)) == ([], True), (
        f"the table's {len(keys)} label and scope keys are not all in the {lang} strings")


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_but_app_js_reads_a_key(name):
    """§3.7: "The page binds keys in four places, **all in js/app.js**". A keyboard read in
    another script is a fifth surface the table does not know about."""
    if name == "app.js":
        pytest.skip("app.js is where §3.7 puts every keyboard handler")
    found = [f"{r.script}:{r.line} {r.source}" for r in keyboard_reads({name: read_js(name)})]
    assert found == [], f"{name} reads a key, which §3.7 says only app.js does: {found}"
