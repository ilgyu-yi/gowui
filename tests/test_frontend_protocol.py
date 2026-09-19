"""The page and the server speak the same WebSocket protocol (SPEC §4.1, §4.2; issue #8 AC3).

Both directions:

- every type the page sends is one the server dispatches, and the server's dispatch table is the
  §4.1 table;
- every type the server emits — every ``{"type": "<literal>", ...}`` dict under ``gowui/``, found
  by an AST walk — is the §4.2 table, and the page handles each one in its ``switch``.

The server half reads the session's dispatch table object; only the JS half uses regular
expressions (tests/frontend_helpers.py), with count guards.
"""

from __future__ import annotations

import ast
import re

import pytest

from frontend_helpers import (REPO, read_js, send_calls, sent_types, spec_table_types,
                              strip_js_comments, switch_block, CASE_LABEL)

#: The §4.1 types the page never sends: SGF goes through POST /api/sgf (§3.8 "SGF"), and the
#: attach frames bring a fresh state without asking.
NOT_SENT_BY_THE_PAGE = {"load_sgf", "state"}


def dispatched() -> set[str]:
    from gowui.session import GameSession

    return set(GameSession._HANDLERS)


def emitted() -> set[str]:
    """Every ``type`` a ``{"type": "<literal>", ...}`` dict literal under ``gowui/`` names.

    Two kinds of such dicts are not frames to the browser and are left out: ASGI messages (their
    type has a dot, ``websocket.close``) and a message handed *into* the session with
    ``session.handle({...})`` (the CLI's ``--connect``).
    """
    types: set[str] = set()
    for path in sorted((REPO / "gowui").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        inbound = {id(arg) for node in ast.walk(tree) if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute) and node.func.attr == "handle"
                   for arg in node.args}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict) or id(node) in inbound:
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant) and key.value == "type"
                        and isinstance(value, ast.Constant) and isinstance(value.value, str)
                        and "." not in value.value):
                    types.add(value.value)
    return types


def test_the_frame_walk_finds_the_session_frames():
    """Count guard on the AST walk: the session builds all five frame types as literals."""
    assert len(emitted()) >= 5


def page_sends() -> set[str]:
    return set(sent_types(read_js("app.js")))


def page_handles() -> set[str]:
    return {m.group("type") for m in CASE_LABEL.finditer(switch_block(read_js("app.js")))}


# -- the server half ------------------------------------------------------------------------------
def test_the_spec_browser_to_server_table_names_the_known_types():
    """Count guard on the SPEC reader: §4.1 names 21 types."""
    assert len(spec_table_types("### 4.1")) == 21


def test_the_server_dispatches_exactly_the_spec_browser_to_server_types():
    assert dispatched() == spec_table_types("### 4.1")


def test_the_spec_server_to_browser_table_names_the_known_types():
    assert spec_table_types("### 4.2") == {"state", "analysis", "log", "log_history", "error"}


def test_the_server_emits_exactly_the_spec_server_to_browser_types():
    assert emitted() == spec_table_types("### 4.2")


# -- the page half ---------------------------------------------------------------------------------
def test_every_send_call_names_its_type_as_a_literal():
    calls = send_calls(read_js("app.js"))
    literal = [c for c in calls if re.match(r"send\s*\(\s*\{\s*type\s*:\s*'[a-z_]+'", c)]
    assert (len(calls) > 0, len(literal) == len(calls)) == (True, True), \
        [c for c in calls if c not in literal]


@pytest.mark.parametrize("name", ["i18n.js", "tuple.js", "profile.js", "board.js"])
def test_only_app_js_sends_frames(name):
    code = strip_js_comments(read_js(name))
    assert re.findall(r"(?<![\w.$])send\s*\(\s*\{", code) == []


def test_every_type_the_page_sends_is_dispatched_by_the_server():
    sends = page_sends()
    assert sends and sorted(sends - dispatched()) == []


def test_the_page_sends_every_control_type_of_the_spec():
    """§3.8 gives each of these a control; the page sends all but ``load_sgf`` and ``state``."""
    assert sorted(spec_table_types("### 4.1") - NOT_SENT_BY_THE_PAGE - page_sends()) == []


def test_the_page_never_sends_load_sgf():
    assert "load_sgf" not in page_sends()


def test_the_page_has_one_switch_over_the_frame_type():
    assert switch_block(read_js("app.js")) != ""


def test_the_page_handles_every_type_the_server_emits():
    handles = page_handles()
    assert handles and sorted(emitted() - handles) == []


def test_the_page_handles_no_type_the_server_never_emits():
    assert sorted(page_handles() - emitted()) == []


# -- field names the page reads (§4.2 "Field names inside state") -------------------------------------
async def test_every_state_field_the_page_reads_is_in_a_real_state_frame(h):
    """Each ``game.X`` / ``settings.X`` / ``engine.X`` the page reads exists in a real ``state``
    frame, so a renamed server field cannot leave the page reading ``undefined``."""
    frame = await h.fresh_state()
    code = strip_js_comments(read_js("app.js"))
    missing = []
    for part in ("game", "settings", "engine"):
        read = set(re.findall(rf"(?<![\w$]){part}\.([A-Za-z_]\w*)", code))
        missing += [f"{part}.{name}" for name in sorted(read - set(frame[part]))]
    assert missing == []


def test_the_page_reads_many_state_fields():
    """Count guard for the field check above: the page reads over 15 game and settings fields."""
    code = strip_js_comments(read_js("app.js"))
    read = set(re.findall(r"(?<![\w$])(?:game|settings)\.([A-Za-z_]\w*)", code))
    assert len(read) > 15


async def test_every_board_entry_field_of_the_spec_is_in_a_real_state_frame(h):
    """§4.2 names each ``boards`` entry's fields; the page's board strip reads them."""
    entry = (await h.fresh_state())["boards"][0]
    wanted = {"id", "name", "size", "stones", "lastMove", "cursor", "moveCount", "toPlay",
              "profile", "policy", "compare", "heat", "winrate"}
    assert sorted(wanted - set(entry)) == []


def test_the_page_reads_the_capability_flags_not_a_protocol_name():
    """§3.8 "Capability gating": final score and the console follow ``state.engine`` flags."""
    code = strip_js_comments(read_js("app.js"))
    flags = {name for name in ("supportsGenmove", "supportsFinalScore", "console")
             if re.search(rf"\bengine\.{name}\b", code)}
    assert flags == {"supportsGenmove", "supportsFinalScore", "console"}


def test_the_page_never_compares_a_launch_mode_name():
    """§6.1: the page adapts to data, never to a mode name."""
    code = "\n".join(strip_js_comments(read_js(n)) for n in
                     ("app.js", "board.js", "tuple.js", "profile.js"))
    mode = r"['\"](?:local|server)['\"]"
    assert re.findall(rf"[!=]==?\s*{mode}|{mode}\s*[!=]==?", code) == []
