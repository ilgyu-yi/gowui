"""Characterisation net for the page's keyboard (SPEC §3.7, §3.8; issue #64 phase 0).

Every keydown the page binds lives in one of four places in ``gowui/static/js/app.js``:

* the document listener of §3.7 (``app.js:1484``) — ``←`` ``→`` ``Home`` ``End`` ``p`` ``u``
  ``g`` ``a`` ``[`` ``]``, behind three guards: a focused ``input``/``select``/``textarea``, a
  held Ctrl/Cmd/Alt, and no game yet;
* a second document listener (``app.js:857``) — ``Escape`` cancels a drag;
* ``tileKey`` (``app.js:843``, bound at ``app.js:555``) — ``Alt``+``↑``/``↓`` reorders a focused
  tile, ``Enter``/``Space`` selects it, and a key whose target is not the tile itself is left to
  whatever is inside it;
* the rename field's own handler (``app.js:648``) — ``Enter`` saves, ``Escape`` discards.

These tests assert the *effect* — a frame sent, a cursor moved, a tile reordered, a name saved —
so that moving this code without moving the behaviour shows up here. Three bindings already have
cover in test_smoke.py (``Alt+ArrowUp`` at :184, ``ArrowLeft`` plus the field exemption at :353,
the Ctrl/Cmd/Alt bail-out at :373) and are not repeated.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import ENGINE, QUICK, expect

pytestmark = pytest.mark.browser


def connected(start_engine, start_app, open_page):
    """The page on an app connected to a fake GTP engine at startup (as test_smoke.py does)."""
    app = start_app(start_engine("gtp"), True)
    g = open_page(app).open()
    expect(g.page.locator("#engine-state")).not_to_have_text(re.compile("disconnected"),
                                                             timeout=ENGINE)
    return g


def tile(g, board_id: int):
    """The strip's tile for ``board_id`` (§3.8: tiles are matched by their ``data-id``)."""
    return g.page.locator(f'#board-list .thumb[data-id="{board_id}"]')


def tile_ids(g) -> list[str]:
    return g.page.evaluate("() => Array.from(document.querySelectorAll('#board-list .thumb'))"
                           ".map((node) => node.dataset.id)")


def two_boards(g) -> tuple[int, int]:
    """A second board, and the ids of the two in strip order."""
    g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)
    first, second = (board["id"] for board in g.state()["boards"])
    return first, second


def played(g, *vertices: str):
    """Play ``vertices`` through the socket (setup, not the keyboard under test)."""
    for v in vertices:
        g.act({"type": "play", "color": g.state()["game"]["toPlay"], "vertex": v})
    expect(g.page.locator("#move-counter")).to_have_text(f"{len(vertices)} / {len(vertices)}")


# -- the document listener: moving through the game (§3.7) ---------------------------------------
def test_arrow_right_steps_one_move_forward(start_app, open_page):
    """§3.7: ``→`` one move forward — the counter's cursor rises by one."""
    g = open_page(start_app()).open()
    played(g, "D4", "Q16")
    g.act({"type": "navigate", "index": 0})
    expect(g.page.locator("#move-counter")).to_have_text("0 / 2")

    g.page.keyboard.press("ArrowRight")
    expect(g.page.locator("#move-counter")).to_have_text("1 / 2")


def test_home_jumps_to_the_first_position(start_app, open_page):
    """§3.7: ``Home`` first position — from the end of a two-move game, back to the empty board."""
    g = open_page(start_app()).open()
    played(g, "D4", "Q16")

    g.page.keyboard.press("Home")
    expect(g.page.locator("#move-counter")).to_have_text("0 / 2")


def test_end_jumps_to_the_last_position(start_app, open_page):
    """§3.7: ``End`` last position — from the empty board, forward to the whole game."""
    g = open_page(start_app()).open()
    played(g, "D4", "Q16")
    g.act({"type": "navigate", "index": 0})
    expect(g.page.locator("#move-counter")).to_have_text("0 / 2")

    g.page.keyboard.press("End")
    expect(g.page.locator("#move-counter")).to_have_text("2 / 2")


def test_a_handled_key_does_not_also_scroll_the_page(start_app, open_page):
    """§3.7: "A handled key does not also perform the browser's default action (no page scroll on
    Home / End)". Narrow enough that the document itself scrolls (§3.8 "Scrolling"), and scrolled
    to the bottom so the browser's own ``Home`` would jump to the top: the key moves the cursor
    and leaves the scroll where it was."""
    g = open_page(start_app()).open()
    g.page.set_viewport_size({"width": 700, "height": 500})
    played(g, "D4", "Q16")
    g.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
    g.until("() => window.scrollY > 0")
    was = g.page.evaluate("() => window.scrollY")

    g.page.keyboard.press("Home")
    expect(g.page.locator("#move-counter")).to_have_text("0 / 2")
    # The browser applies its own scroll a frame or two after the key, not inside `press`.
    g.page.wait_for_timeout(500)
    assert g.page.evaluate("() => window.scrollY") == was, \
        f"Home scrolled the page from {was} to {g.page.evaluate('() => window.scrollY')}"


# -- the document listener: playing (§3.7) -------------------------------------------------------
def test_p_passes(start_app, open_page):
    """§3.7: ``p`` pass — a pass is a move, so the fresh game grows one."""
    g = open_page(start_app()).open()
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")

    g.page.keyboard.press("p")
    expect(g.page.locator("#move-counter")).to_have_text("1 / 1")


def test_u_undoes_the_last_move(start_app, open_page):
    """§3.7: ``u`` undo — the move played is taken back off the board."""
    g = open_page(start_app()).open()
    played(g, "D4")

    g.page.keyboard.press("u")
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_g_asks_the_engine_for_a_move(start_engine, start_app, open_page):
    """§3.7: ``g`` engine move — the connected engine's move lands on the board."""
    g = connected(start_engine, start_app, open_page)
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")

    g.page.keyboard.press("g")
    expect(g.page.locator("#move-counter")).to_have_text("1 / 1", timeout=ENGINE)


def test_a_toggles_analysis(start_app, open_page):
    """§3.7: ``a`` toggle analysis — the box in the Analysis section follows, and the server is
    told, because the box alone would leave the engine idle."""
    g = open_page(start_app()).open()
    expect(g.page.locator("#analysis-on")).not_to_be_checked()

    since = g.mark()
    g.page.keyboard.press("a")
    assert (g.wait_sent("analysis", since)["enabled"],
            g.page.locator("#analysis-on").is_checked()) == (True, True)


# -- the document listener: the board strip (§3.7 "[ / ] previous / next board") ------------------
def test_bracket_right_switches_to_the_next_board(start_app, open_page):
    """§3.7: ``]`` next board — the tile after the active one becomes active."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    g.act({"type": "board_select", "id": first})
    expect(tile(g, first)).to_have_class(re.compile(r"\bactive\b"))

    g.page.keyboard.press("]")
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))


def test_bracket_left_switches_to_the_previous_board(start_app, open_page):
    """§3.7: ``[`` previous board — "+ New board" leaves the second tile active, so ``[`` goes
    back to the first."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))

    g.page.keyboard.press("[")
    expect(tile(g, first)).to_have_class(re.compile(r"\bactive\b"))


def test_bracket_left_on_the_first_board_does_nothing(start_app, open_page):
    """Boundary: there is no board before the first, so ``[`` there selects nothing — it must not
    wrap round to the last one."""
    g = open_page(start_app()).open()
    first, _ = two_boards(g)
    g.act({"type": "board_select", "id": first})
    expect(tile(g, first)).to_have_class(re.compile(r"\bactive\b"))

    since = g.mark()
    g.page.keyboard.press("[")
    g.fence()
    assert g.sent(since, "board_select") == []


# -- the document listener's guards (§3.7) -------------------------------------------------------
def test_a_shortcut_on_a_focused_select_does_not_act(start_app, open_page):
    """§3.7: "A key is ignored while an ``input``, ``select`` or ``textarea`` has focus".
    test_smoke.py covers the ``input`` arm; the language select is the ``select`` arm, where ``p``
    is the select's own type-ahead and not a pass."""
    g = open_page(start_app()).open()
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")

    g.page.locator("#lang").focus()
    since = g.mark()
    g.page.keyboard.press("p")
    g.fence()
    assert g.sent(since, "pass") == []


def test_a_shortcut_typed_into_the_rename_field_is_just_text(start_app, open_page):
    """§3.7 names the board rename field among the fields a key is ignored in (§3.8) — so ``[``
    there is a bracket in the name, not a board switch.

    Two lines hold this up: the field's own ``stopPropagation`` (``app.js:649``) and the document
    listener's ``INPUT`` guard (``app.js:1485``). Either alone is enough, so this test only turns
    red when both go — which is the honest shape of the behaviour, not a weakness in the test."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    tile(g, second).locator(".thumb-edit").click()
    field = tile(g, second).locator(".thumb-rename")
    field.fill("study")

    since = g.mark()
    g.page.keyboard.press("[")
    g.fence()
    assert (g.sent(since, "board_select"), field.input_value()) == ([], "study[")


def test_no_shortcut_acts_before_the_first_state(start_app, open_page):
    """§3.7: a key is ignored "before the first ``state`` has arrived". The socket is routed into
    the test and answers nothing, so the page is connected with no game — and ``p`` must not send
    a pass into that."""
    g = open_page(start_app())
    # A mock socket: it opens (so the page *can* send) and never delivers a state.
    g.page.route_web_socket(re.compile(r".*/ws$"), lambda route: None)
    g.goto()
    g.until("() => window.__gowuiTest.sockets.length > 0"
            " && window.__gowuiTest.sockets[0].readyState === 1")

    since = g.mark()
    g.page.keyboard.press("p")
    g.page.wait_for_timeout(500)   # no state comes back, so there is no fence to round-trip
    assert g.sent(since) == []


# -- the second document listener: Escape cancels a drag (app.js:857) ----------------------------
def test_escape_during_a_drag_drops_the_tile_where_it_was(start_app, open_page):
    """§3.8 "Reordering": "Escape or a cancelled pointer ends the drag and sends nothing". The
    same drag that reorders in test_smoke.py, with Escape before the release."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    start, target = tile(g, first).bounding_box(), tile(g, second).bounding_box()

    since = g.mark()
    g.page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    g.page.mouse.down()
    g.page.mouse.move(target["x"] + target["width"] / 2, target["y"] + target["height"] - 2,
                      steps=8)
    g.until("() => document.querySelector('#board-list .thumb.dragging') !== null")
    g.page.keyboard.press("Escape")
    g.page.mouse.up()

    g.fence()
    assert (g.sent(since, "board_move"), tile_ids(g)) == ([], [str(first), str(second)])


# -- tileKey (app.js:843) ------------------------------------------------------------------------
def test_alt_arrow_down_moves_a_focused_tile_one_place_later(start_app, open_page):
    """§3.8 "Reordering by keyboard": Alt with the down arrow is the twin of test_smoke.py's
    Alt+ArrowUp — the first tile moves after the second."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)

    since = g.mark()
    tile(g, first).focus()
    g.page.keyboard.press("Alt+ArrowDown")
    frame = g.wait_sent("board_move", since)
    g.until("(ids) => Array.from(document.querySelectorAll('#board-list .thumb'))"
            ".map((node) => node.dataset.id).join() === ids", f"{second},{first}")
    assert (frame["id"], frame["after"]) == (first, second)


def test_alt_arrow_up_on_the_first_tile_does_nothing(start_app, open_page):
    """Boundary (``moveTileBy``: "at the ends it does nothing"): there is nowhere earlier than the
    head, so the frame is not sent at all rather than sent and refused."""
    g = open_page(start_app()).open()
    first, _ = two_boards(g)

    since = g.mark()
    tile(g, first).focus()
    g.page.keyboard.press("Alt+ArrowUp")
    g.fence()
    assert g.sent(since, "board_move") == []


def test_enter_on_a_focused_tile_selects_its_board(start_app, open_page):
    """§3.8: a tile is reachable by keyboard — ``Enter`` on it does what a click does."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))

    tile(g, first).focus()
    g.page.keyboard.press("Enter")
    expect(tile(g, first)).to_have_class(re.compile(r"\bactive\b"))


def test_space_on_a_focused_tile_selects_its_board(start_app, open_page):
    """§3.8: the tile is a button in all but name, so ``Space`` selects as ``Enter`` does."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))

    tile(g, first).focus()
    g.page.keyboard.press(" ")
    expect(tile(g, first)).to_have_class(re.compile(r"\bactive\b"))


def test_enter_on_a_button_inside_a_tile_does_not_also_select_it(start_app, open_page):
    """``tileKey``'s first line: "a button or the rename field answers for itself". ``Enter`` on
    the inactive tile's ⧉ duplicates that board and must not also switch to the tile under it."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))

    since = g.mark()
    tile(g, first).locator(".thumb-duplicate").focus()
    g.page.keyboard.press("Enter")
    g.wait_sent("board_duplicate", since)
    g.fence()
    assert g.sent(since, "board_select") == []


def test_ctrl_enter_on_a_focused_tile_is_left_to_the_browser(start_app, open_page):
    """``tileKey`` bails while Ctrl/Cmd/Alt is held, as the document handler does (§3.7) — so
    Ctrl+Enter on an inactive tile selects nothing."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)
    expect(tile(g, second)).to_have_class(re.compile(r"\bactive\b"))

    since = g.mark()
    tile(g, first).focus()
    g.page.keyboard.press("Control+Enter")
    g.fence()
    assert g.sent(since, "board_select") == []


def test_alt_arrow_reorders_even_while_ctrl_is_held(start_app, open_page):
    """Characterised, not endorsed: ``tileKey`` runs its ``Alt``+arrow branch *before* the
    Ctrl/Cmd/Alt bail-out (``app.js:845`` above ``app.js:850``), so Ctrl+Alt+↓ reorders too,
    unlike every other tile key. Pinned here so a refactor that reorders those two lines is a
    decision someone takes rather than one that happens."""
    g = open_page(start_app()).open()
    first, second = two_boards(g)

    since = g.mark()
    tile(g, first).focus()
    g.page.keyboard.press("Control+Alt+ArrowDown")
    frame = g.wait_sent("board_move", since)
    assert (frame["id"], frame["after"]) == (first, second)


# -- the rename field's own handler (app.js:648) -------------------------------------------------
def test_enter_in_the_rename_field_saves_the_name(start_app, open_page):
    """§3.8 "Rename in place": ``Enter`` commits — the tile shows the typed name and the server
    is told."""
    g = open_page(start_app()).open()
    only = g.state()["activeBoard"]
    tile(g, only).locator(".thumb-edit").click()
    tile(g, only).locator(".thumb-rename").fill("opening study")

    since = g.mark()
    g.page.keyboard.press("Enter")
    assert g.wait_sent("board_rename", since)["name"] == "opening study"


def test_escape_in_the_rename_field_discards_the_name(start_app, open_page):
    """§3.8 "Rename in place": ``Escape`` abandons the edit — the field closes and nothing is
    renamed."""
    g = open_page(start_app()).open()
    only = g.state()["activeBoard"]
    before = tile(g, only).locator(".thumb-name").text_content()
    tile(g, only).locator(".thumb-edit").click()
    tile(g, only).locator(".thumb-rename").fill("never mind")

    since = g.mark()
    g.page.keyboard.press("Escape")
    expect(tile(g, only).locator(".thumb-rename")).to_have_count(0, timeout=QUICK)
    g.fence()
    assert (g.sent(since, "board_rename"),
            tile(g, only).locator(".thumb-name").text_content()) == ([], before)
