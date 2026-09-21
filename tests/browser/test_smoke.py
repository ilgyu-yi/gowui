"""Browser smoke run (issue #8 AC4, #50; SPEC §3.8): against the local app and the fake engine, the
page shows the board, a candidate overlay once analysis is on, and a second board after a tile's ⧉
— with no console error and no CSP violation (§7.5). Screenshots go to ``test-artifacts/``.

The board strip gets smoke cover only: the per-tile ⧉, "+ New board", a drag and its keyboard
twin, and × on the only tile. Every per-frame rule those actions send (§4.1 "The board frames")
is pinned in tests/test_session_boards.py, where it costs no browser.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import ENGINE, expect

pytestmark = pytest.mark.browser


def connected(start_engine, start_app, open_page):
    """The page on an app connected to a fake GTP engine at startup (opened in the test body, so
    a page that is not built fails the test rather than erroring its setup)."""
    app = start_app(start_engine("gtp"), True)
    g = open_page(app).open()
    expect(g.page.locator("#engine-state")).not_to_have_text(re.compile("disconnected"),
                                                              timeout=ENGINE)
    return g


def test_the_page_shows_the_board(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#board")).to_be_visible()


def test_the_board_canvas_records_its_draws(start_app, open_page):
    g = open_page(start_app()).open()
    g.expect_dataset("draws", re.compile(r"^[1-9]\d*$"))


def test_the_board_is_the_fresh_19x19_game(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_analysis_draws_a_candidate_overlay(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    g.screenshot("smoke-analysis")


def test_analysis_fills_the_candidate_table(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)


def tile(g, board_id: int):
    """The strip's tile for ``board_id`` (§3.8: tiles are matched by their ``data-id``)."""
    return g.page.locator(f'#board-list .thumb[data-id="{board_id}"]')


def tile_ids(g) -> list[str]:
    return g.page.evaluate("() => Array.from(document.querySelectorAll('#board-list .thumb'))"
                           ".map((node) => node.dataset.id)")


def test_duplicate_shows_a_second_board(start_app, open_page):
    g = open_page(start_app()).open()
    tile(g, g.state()["activeBoard"]).locator(".thumb-duplicate").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)


def test_the_duplicate_is_the_active_board(start_app, open_page):
    g = open_page(start_app()).open()
    tile(g, g.state()["activeBoard"]).locator(".thumb-duplicate").click()
    expect(g.page.locator("#board-list .thumb").nth(1)).to_have_class(re.compile(r"\bactive\b"))


# -- the board strip (#50; §3.8 "Board strip", §3.3) ----------------------------------------------
def test_the_tile_duplicate_names_that_tiles_board(start_app, open_page):
    """§3.8: ⧉ sends ``board_duplicate`` for *that* tile — §4.1: the frame has no "the active
    one" meaning, so the second tile's ⧉ names the second board, not the active one."""
    g = open_page(start_app()).open()
    g.page.locator("#board-new").click()
    g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(3)
    second = g.state()["boards"][1]["id"]

    since = g.mark()
    tile(g, second).locator(".thumb-duplicate").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(4)
    assert g.wait_sent("board_duplicate", since)["id"] == second


def test_the_new_board_button_opens_an_empty_board(start_app, open_page):
    """§3.8: the button under the strip is "+ New board"; §3.3: it appends a fresh board — an
    empty game — and switches to it."""
    g = open_page(start_app()).open()
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    expect(g.page.locator("#move-counter")).to_have_text("1 / 1")

    g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_dragging_a_tile_past_the_next_one_moves_it(start_app, open_page):
    """§3.8 "Reordering": dropping sends ``board_move`` with the tile the drop landed after, and
    the tile moves when the server's ``state`` comes back."""
    g = open_page(start_app()).open()
    g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)
    first, second = (board["id"] for board in g.state()["boards"])
    start, target = tile(g, first).bounding_box(), tile(g, second).bounding_box()

    since = g.mark()
    g.page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    g.page.mouse.down()
    # The intermediate steps are what cross the drag threshold (§3.8: a press picks nothing up
    # until the pointer has moved a few pixels), so a single jump would not start a drag.
    g.page.mouse.move(target["x"] + target["width"] / 2, target["y"] + target["height"] - 2,
                      steps=8)
    g.page.mouse.up()
    frame = g.wait_sent("board_move", since)
    g.until("(ids) => Array.from(document.querySelectorAll('#board-list .thumb'))"
            ".map((node) => node.dataset.id).join() === ids", f"{second},{first}")
    assert (frame["id"], frame["after"]) == (first, second)


def test_a_double_click_on_the_name_still_opens_the_rename_field(start_app, open_page):
    """§3.8 "Reordering": a press picks nothing up until the pointer has moved a few pixels, so a
    double-click on the name is still itself — and §3.8 "Rename in place" still swaps the name for
    a text field. Capturing the pointer on `pointerdown` retargets the compatibility `dblclick` to
    the tile, and the name's own handler never runs."""
    g = open_page(start_app()).open()
    only = g.state()["activeBoard"]
    tile(g, only).locator(".thumb-name").dblclick()
    expect(tile(g, only).locator(".thumb-rename")).to_have_count(1)


def test_the_edge_scroll_moves_the_drop_target_with_the_strip(start_app, open_page):
    """§3.8 "Reordering": dragging near an edge scrolls the strip "so a tile can be moved past the
    ones that fit on screen". Scrolling without moving the drop target does not serve that: the
    line would stay on the tile the pointer was over before the strip moved, and the frame would
    name the pre-scroll anchor."""
    g = open_page(start_app()).open()
    for _ in range(11):
        g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(12)
    ids = [board["id"] for board in g.state()["boards"]]

    # Each new board becomes active and is scrolled into view, so the strip is already at the
    # bottom and the first tile is off-screen. Put it back, or the press lands on nothing.
    g.page.evaluate("() => { document.querySelector('.boards').scrollTop = 0; }")
    g.until("() => document.querySelector('.boards').scrollTop === 0")
    start = tile(g, ids[0]).bounding_box()
    # The scroller, not #board-list: the list is as tall as all twelve tiles, so its lower edge is
    # far below the window and a point near it is not in the strip at all.
    strip = g.page.locator(".boards").bounding_box()
    since = g.mark()
    g.page.mouse.move(start["x"] + start["width"] / 2, start["y"] + start["height"] / 2)
    g.page.mouse.down()
    # Into the trailing edge band, then held still: the auto-scroll is the only thing moving.
    g.page.mouse.move(start["x"] + start["width"] / 2, strip["y"] + strip["height"] - 8, steps=8)
    marked = ("() => { const tile = document.querySelector('#board-list .thumb.drop-after');"
              " return tile ? Number(tile.dataset.id) : null; }")
    before = g.page.evaluate(marked)
    g.until("(was) => { const tile = document.querySelector('#board-list .thumb.drop-after');"
            " return tile !== null && Number(tile.dataset.id) !== was; }", before)
    after = g.page.evaluate(marked)
    g.page.mouse.up()

    frame = g.wait_sent("board_move", since)
    # `after != before` is already what the wait above gated on, so the claim left to make is
    # that the frame carries the anchor the line moved to, not the one it started on.
    assert frame["after"] == after, (
        f"the drop line moved from board {before} to {after} as the strip scrolled, "
        f"but the move named {frame['after']}")


def test_alt_arrow_up_moves_a_focused_tile_one_place_earlier(start_app, open_page):
    """§3.8 "Reordering by keyboard": Alt with the up arrow moves a focused tile one place
    earlier, sending the same ``board_move`` — the second tile's anchor is the head."""
    g = open_page(start_app()).open()
    g.page.locator("#board-new").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)
    first, second = (board["id"] for board in g.state()["boards"])

    since = g.mark()
    tile(g, second).focus()
    g.page.keyboard.press("Alt+ArrowUp")
    frame = g.wait_sent("board_move", since)
    g.until("(ids) => Array.from(document.querySelectorAll('#board-list .thumb'))"
            ".map((node) => node.dataset.id).join() === ids", f"{second},{first}")
    assert (frame["id"], frame["after"]) == (second, None)


def test_the_close_on_the_only_tile_resets_the_board_in_place(start_app, open_page):
    """§3.3 "Delete": deleting the last board resets it in place — the tile stays, with the same
    id, so a tab holding that id does not lose it."""
    g = open_page(start_app()).open()
    only = g.state()["boards"][0]["id"]
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    expect(g.page.locator("#move-counter")).to_have_text("1 / 1")

    g.page.once("dialog", lambda dialog: dialog.accept())
    tile(g, only).locator(".thumb-close").click()
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")
    expect(g.page.locator("#board-list .thumb")).to_have_count(1)
    assert tile_ids(g) == [str(only)]


def test_the_smoke_flow_end_to_end(start_engine, start_app, open_page):
    """The whole AC4 flow in one page, with the screenshots the pull request shows."""
    g = connected(start_engine, start_app, open_page)
    g.screenshot("smoke-board")
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    tile(g, g.state()["activeBoard"]).locator(".thumb-duplicate").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)
    expect(g.page.locator("#board-list .thumb").nth(1)).to_have_class(re.compile(r"\bactive\b"))
    g.screenshot("smoke-duplicate")
    assert g.problems() == []


def test_hovering_a_candidate_row_previews_its_pv(start_engine, start_app, open_page):
    """B5 (§3.8 "PV preview"): hovering a candidate-table row draws its PV; leaving clears it."""
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    row = g.page.locator("#candidates tr").first
    expect(row).to_be_visible(timeout=ENGINE)
    row.hover()
    g.expect_dataset("preview", re.compile(r"^[A-HJ-T]\d{1,2}$"))
    g.expect_dataset("previewStones", re.compile(r"^[1-9]\d*$"))
    g.page.mouse.move(0, 0)
    g.expect_dataset("preview", "")
    g.expect_dataset("previewStones", "0")


# -- the candidate readout (#44; §3.8 "Candidate readout", "The position recedes…") --------------
#: The one line under the board. Its fields are named ``data-field`` so a test can read one
#: without pinning the order or the wording of the line around it.
READOUT = "#candidate-readout"


def analysing(start_engine, start_app, open_page):
    """A connected page with analysis on, the candidates drawn and the table filled."""
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    return g


def shown(g):
    """The readout line itself, asserted to be on the page: the premise a field reading needs,
    since a field of a line that is not there would read ``None`` for the wrong reason."""
    expect(g.page.locator(READOUT)).to_be_visible()


def field(g, name: str):
    """The readout's ``name`` field, or ``None`` when the line has no such field."""
    return g.page.evaluate("""(name) => {
        const node = document.querySelector('#candidate-readout [data-field="' + name + '"]');
        return node ? node.textContent.trim() : null;
    }""", name)


def row_move(g, index: int) -> str:
    """The move in row ``index`` of the candidate table."""
    cell = g.page.locator("#candidates tr").nth(index).locator("td").first
    return (cell.text_content() or "").strip()


def a_number(text) -> bool:
    return bool(text) and bool(re.search(r"\d", text))


def dim(g):
    """``positionDim`` (§3.8 "Test observability"), or ``None`` when the draw recorded none."""
    try:
        return float(g.dataset("positionDim"))
    except (TypeError, ValueError):
        return None


def test_the_readout_names_the_best_candidate(start_engine, start_app, open_page):
    """§3.8 "Candidate readout": with no pointer on a circle or a row the line shows the best
    candidate — the move of the table's first row, the one marked as the best."""
    g = analysing(start_engine, start_app, open_page)
    shown(g)
    assert field(g, "move") == row_move(g, 0)


def test_the_readout_shows_visits_and_value_as_numbers(start_engine, start_app, open_page):
    """§3.8 "Candidate readout": the line carries the table mode's fields plus Visits and Value.
    A value the engine did not report reads ``-``; the fake engine (§2.6) reports both a visit
    count and a `utility`, so on this page neither field is a dash."""
    g = analysing(start_engine, start_app, open_page)
    shown(g)
    visits, value = field(g, "visits"), field(g, "value")
    assert (a_number(visits), a_number(value)) == (True, True), (visits, value)


def test_hovering_a_row_moves_the_readout_and_leaving_puts_it_back(start_engine, start_app,
                                                                   open_page):
    """§3.8 "Candidate readout" and "PV preview": the line shows the candidate under the pointer,
    on a row as on a circle, and with no pointer on either it is back on the best candidate —
    the same line in both cases, with and without a preview up."""
    g = analysing(start_engine, start_app, open_page)
    shown(g)
    best, hovered = row_move(g, 0), row_move(g, 1)
    assert best != hovered, f"the first two candidates are the same move ({best})"

    before = (field(g, "move"), g.dataset("preview"))
    g.page.locator("#candidates tr").nth(1).hover()
    g.expect_dataset("preview", hovered)
    during = (field(g, "move"), g.dataset("preview"))
    g.page.mouse.move(0, 0)
    g.expect_dataset("preview", "")
    after = (field(g, "move"), g.dataset("preview"))
    assert (before, during, after) == ((best, ""), (hovered, hovered), (best, ""))


def test_the_position_recedes_while_a_preview_is_up(start_engine, start_app, open_page):
    """§3.8 "The position recedes while a preview is up": the variation is drawn at full strength
    and the position behind it is dimmed — so ``positionDim`` is 1 with no preview, below 1 while
    one is up, and 1 again once it is gone."""
    g = analysing(start_engine, start_app, open_page)
    full = dim(g)
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("preview", re.compile(r"^[A-HJ-T]\d{1,2}$"))
    dimmed = dim(g)
    g.page.mouse.move(0, 0)
    g.expect_dataset("preview", "")
    assert (full, dimmed is not None and dimmed < 1, dim(g)) == (1.0, True, 1.0), \
        f"positionDim went {full} -> {dimmed} -> {dim(g)}"


def test_arrow_left_steps_back_unless_an_input_has_focus(start_app, open_page):
    """B28 (§3.7): ``ArrowLeft`` sends ``navigate`` one move back; typing in a field does not."""
    g = open_page(start_app()).open()
    for v in ("D4", "Q16"):
        g.act({"type": "play", "color": g.state()["game"]["toPlay"], "vertex": v})
    expect(g.page.locator("#move-counter")).to_have_text("2 / 2")

    g.page.locator("#host").focus()
    since = g.mark()
    g.page.keyboard.press("ArrowLeft")
    g.fence()
    assert g.sent(since, "navigate") == []
    expect(g.page.locator("#move-counter")).to_have_text("2 / 2")

    g.page.locator("#host").blur()
    since = g.mark()
    g.page.keyboard.press("ArrowLeft")
    assert g.wait_sent("navigate", since)["index"] == 1
    expect(g.page.locator("#move-counter")).to_have_text("1 / 2")


def test_a_shortcut_with_ctrl_or_cmd_is_left_to_the_browser(start_app, open_page):
    """B28 (§3.7): Ctrl/Cmd combinations such as Ctrl+P (print) send no game frame."""
    g = open_page(start_app()).open()
    since = g.mark()
    for combo in ("Control+p", "Control+g", "Meta+p", "Alt+u"):
        g.page.keyboard.press(combo)
    g.fence()
    assert [f["type"] for f in g.sent(since) if f["type"] != "state"] == []  # the fence sends a state

