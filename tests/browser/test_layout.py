"""What scrolls, and what does not, at each width (SPEC §3.8 "Scrolling", "Board strip"; issue
#45).

The checks, one per rule the §3.8 "Scrolling" paragraph carries: above 980px the document does not
scroll and each column scrolls inside itself with the board standing still; a window too short for
the side panel still reaches every control, by a column's scrolling and never by the page's; at
980px and below the page scrolls as one column while the board strip scrolls sideways within
itself; and from 320px up the page scrolls down and never across (issue #48).

The viewport here is deliberately shorter than the shared context's 1400x1000, because the rule is
only visible in a window the content does not fit. Every read after a viewport change waits on a
layout predicate — the board's resize handler runs on its own frame, so a box read before it is the
old one.
"""

from __future__ import annotations

import pytest

from browser_kit import (QUICK, Gowui, analysis_frame, analysis_payload, expect, move_info,
                         vertex)

pytestmark = pytest.mark.browser

#: Wide side of the 980px breakpoint, and short enough that the side panel cannot fit.
SHORT = {"width": 1100, "height": 560}
#: Narrow side of the breakpoint.
NARROW = {"width": 900, "height": 700}


def settled(g: Gowui) -> int:
    """The board's draw count, once two polls in a row read the same one (no sleep: a redraw
    bumps ``dataset.draws``, so "stopped redrawing" is a predicate)."""
    g.page.evaluate("() => { window.__settleDraws = -1; }")
    g.until("""() => {
        const drawn = Number(document.getElementById('board').dataset.draws);
        const same = drawn === window.__settleDraws;
        window.__settleDraws = drawn;
        return same;
    }""")
    return g.draws()


def resized(g: Gowui, size: dict) -> None:
    """Apply ``size`` and wait until the page has laid out again for it."""
    g.page.set_viewport_size(size)
    g.until("(size) => window.innerWidth === size.width && window.innerHeight === size.height",
            size)
    settled(g)


def fill_strip(g: Gowui, count: int) -> None:
    """Fill the strip with ``count`` boards through the page's own "+ New board" button."""
    tiles = g.page.locator("#board-list .thumb")
    for wanted in range(tiles.count() + 1, count + 1):
        g.page.locator("#board-new").click()
        expect(tiles).to_have_count(wanted, timeout=QUICK)
    settled(g)


def board_corner(g: Gowui) -> tuple[float, float]:
    """The board canvas's top left on the page."""
    box = g.page.locator("#board").bounding_box()
    assert box is not None, "the board is not rendered"
    return box["x"], box["y"]


def scrolled(g: Gowui, selector: str, axis: str = "Top") -> float:
    """Push ``selector``'s own scroller to its end and return how far it went."""
    return g.page.evaluate("""([selector, axis]) => {
        const box = document.querySelector(selector);
        box['scroll' + axis] = axis === 'Top' ? box.scrollHeight : box.scrollWidth;
        return box['scroll' + axis];
    }""", [selector, axis])


def page_scroll(g: Gowui) -> float:
    return g.page.evaluate("() => window.scrollY")


def document_room(g: Gowui) -> tuple[float, float]:
    """The document's scroll height and the viewport's height."""
    return tuple(g.page.evaluate("() => [document.documentElement.scrollHeight,"
                                 " document.documentElement.clientHeight]"))


def test_the_board_does_not_move_while_a_side_column_scrolls(start_app, open_page):
    """§3.8 "Scrolling", wide: the document does not scroll and each of the three columns scrolls
    inside itself, so scrolling the board strip or the side panel moves nothing in the board
    pane — and the board keeps its size rather than resizing under the scrollbar."""
    g = open_page(start_app()).open()
    resized(g, SHORT)
    fill_strip(g, 6)

    g.page.evaluate("() => window.scrollTo(0, 500)")
    assert page_scroll(g) == 0, "the document scrolled"
    height, viewport = document_room(g)
    assert height <= viewport + 1, f"the document is {height}px tall in a {viewport}px viewport"

    was = board_corner(g)
    for column in (".side", ".boards"):
        assert scrolled(g, column) > 0, f"{column} does not scroll inside itself"
        settled(g)
        now = board_corner(g)
        assert max(abs(now[0] - was[0]), abs(now[1] - was[1])) < 0.5, (
            f"the board moved from {was} to {now} while {column} scrolled")

    bitmap, wanted = g.page.evaluate("""() => {
        const canvas = document.getElementById('board');
        return [canvas.width, Math.round(canvas.clientWidth * (window.devicePixelRatio || 1))];
    }""")
    assert bitmap == wanted, f"the board's bitmap is {bitmap}px wide, not {wanted}px"
    drew = settled(g)
    assert g.draws() == drew, "the board is still redrawing after the columns settled"


def test_a_short_window_still_reaches_every_control(start_app, open_page):
    """§3.8 "Scrolling": every control is reachable in a window short enough to need scrolling,
    by its column's scrolling and never by the page's, and a control reached by keyboard is
    scrolled into view inside its own column so the board does not move."""
    g = open_page(start_app()).open()
    resized(g, SHORT)
    g.page.evaluate("() => document.querySelectorAll('.side details')"
                    ".forEach((section) => { section.open = true; })")
    settled(g)

    for control in ("#raw", "#resign"):
        g.page.locator(control).scroll_into_view_if_needed()
        expect(g.page.locator(control)).to_be_in_viewport(timeout=QUICK)
        assert page_scroll(g) == 0, f"the page scrolled to bring {control} into view"

    # The keyboard leg uses the Send button beside #raw, the lowest control in the side panel that
    # can take focus: #raw itself is disabled while no engine console is open (§3.8 "Engine
    # console"), and a disabled field never becomes the active element.
    g.page.evaluate("() => { window.scrollTo(0, 0);"
                    " document.querySelector('.side').scrollTop = 0; }")
    was = board_corner(g)
    g.page.locator("#raw-form button").focus()
    assert g.page.evaluate("() => document.querySelector('.side').scrollTop") > 0, (
        "focusing a control low in the side panel scrolled no column")
    assert page_scroll(g) == 0, "focusing a control in the side panel scrolled the page"
    now = board_corner(g)
    assert max(abs(now[0] - was[0]), abs(now[1] - was[1])) < 0.5, (
        f"the board moved from {was} to {now} while a control took focus")

    # A column that scrolls vertically treats a horizontal overflow as scrollable too, so anything
    # inside it that is wider than the column hands the panel a sideways scrollbar (§3.8
    # "Scrolling"). The handol-mux panel is where that bites: its Preset row holds a select that
    # does not shrink on its own.
    g.page.evaluate("() => document.querySelectorAll('.human-only')"
                    ".forEach((section) => { section.hidden = false; section.open = true; })")
    settled(g)
    room = g.page.evaluate("() => { const side = document.querySelector('.side');"
                           " return [side.scrollWidth, side.clientWidth]; }")
    assert room[0] == room[1], (
        f"the side panel is {room[0]}px wide inside a {room[1]}px column and scrolls sideways")


def test_the_comparing_columns_do_not_widen_the_side_panel(start_app, open_page):
    """§3.8 "The page scrolls down, never across" with "Candidate table": while comparing, the
    table carries eight columns. A column that scrolls vertically treats a horizontal overflow as
    scrollable too, so a table wider than the side panel hands the panel a sideways scrollbar —
    and nothing may be reached only by scrolling across."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    resized(g, SHORT)
    size = g.state()["game"]["size"]
    infos = [move_info(vertex(x, 0, size), visits=1000 - x) for x in range(10)]
    flat = [1.0 / (size * size + 1)] * (size * size + 1)
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, infos, source="handol", policy=flat,
        compare={"policy": flat, "moveInfos": infos})))
    expect(g.page.locator("table.candidates thead th")).to_have_count(8, timeout=QUICK)

    room = g.page.evaluate("() => { const side = document.querySelector('.side');"
                           " return [side.scrollWidth, side.clientWidth]; }")
    assert room[0] == room[1], (
        f"the side panel is {room[0]}px wide inside a {room[1]}px column and scrolls sideways "
        "with the comparing table's eight columns")


def test_the_narrow_layout_scrolls_as_one_column(start_app, open_page):
    """§3.8 "Scrolling", narrow: at 980px and below the layout is one column and the page scrolls
    as a whole, while the board strip lies across the top and scrolls sideways within itself
    (§3.8 "Board strip")."""
    g = open_page(start_app()).open()
    resized(g, NARROW)
    fill_strip(g, 8)

    height, viewport = document_room(g)
    assert height > viewport, f"the narrow page is {height}px tall and does not scroll"
    g.page.evaluate("() => window.scrollTo(0, 200)")
    assert page_scroll(g) > 0, "the narrow page did not scroll as a whole"

    width, across = g.page.evaluate("""() => {
        const strip = document.querySelector('.board-list');
        return [strip.scrollWidth, strip.clientWidth];
    }""")
    assert width > across, f"the strip is {width}px across a {across}px column and cannot scroll"
    before = page_scroll(g)
    assert scrolled(g, ".board-list", "Left") > 0, "the strip does not scroll sideways"
    assert page_scroll(g) == before, "scrolling the strip sideways moved the page"


#: Narrow enough that a control row must wrap and the board must be well under its 78vh cap.
CRAMPED = {"width": 360, "height": 700}
#: The narrowest width §3.8 "Scrolling" claims; under about 260px the board's own 240px floor no
#: longer fits and the page does scroll across, which SPEC says is accepted rather than designed for.
FLOOR = {"width": 320, "height": 700}


def widest_overflow(g: Gowui) -> list[str]:
    """Every element whose right edge is past the document's, nearest first."""
    return g.page.evaluate("""() => {
        const edge = document.documentElement.clientWidth;
        return [...document.querySelectorAll('*')]
            .filter((el) => el.getBoundingClientRect().right > edge + 1)
            .map((el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '')
                         + (typeof el.className === 'string' && el.className
                            ? '.' + el.className.split(' ')[0] : ''))
            .slice(0, 6);
    }""")


def test_the_board_comes_back_down_when_the_window_does(start_app, open_page):
    """§3.8 "Scrolling": the board takes the width its column offers and no more. Its size is
    written in pixels from the width it measured, so a column allowed to be as wide as its own
    contents would let it keep whatever width it once reached."""
    g = open_page(start_app()).open()
    resized(g, {"width": 1400, "height": 1000})
    grown = g.page.locator("#board-wrap").bounding_box()["width"]
    assert grown > 700, f"the board did not grow at a large window: {grown}px"

    resized(g, CRAMPED)
    column = g.page.evaluate("() => document.querySelector('.board-pane').clientWidth")
    board = g.page.locator("#board-wrap").bounding_box()["width"]
    assert board < grown, f"the board stayed {board}px after the window shrank from {grown}px"
    # Not just "inside its column": at this width the column is what sizes the board, so a board
    # that shrank too far would pass the weaker check. `board < grown` alone would let a 1px board
    # through.
    assert abs(board - column) <= 1, f"the board is {board}px inside a {column}px column"
    bitmap, across, ratio = g.page.evaluate(
        "() => { const c = document.querySelector('#board');"
        " return [c.width, c.clientWidth, devicePixelRatio]; }")
    assert bitmap == round(across * ratio), (
        f"the canvas is {bitmap} device px for {across} CSS px at dpr {ratio}")


def test_the_page_never_scrolls_across(start_app, open_page):
    """§3.8 "Scrolling": from 320px up, nothing may be reached only by scrolling the page
    sideways — so a control row too wide for the window wraps instead of pushing past it. Below
    about 260px the board's own 240px floor stops fitting and SPEC accepts the overflow, which is
    why the sweep stops at the floor rather than going lower."""
    g = open_page(start_app()).open()
    for size in ({"width": 1400, "height": 1000}, NARROW, CRAMPED, FLOOR):
        resized(g, size)
        across, within, pane, column = g.page.evaluate(
            "() => { const p = document.querySelector('.board-pane');"
            " return [document.documentElement.scrollWidth,"
            " document.documentElement.clientWidth, p.scrollWidth, p.clientWidth]; }")
        assert across <= within + 1, (
            f"at {size['width']}x{size['height']} the page is {across}px across a {within}px "
            f"window; past the edge: {widest_overflow(g)}")
        assert pane <= column + 1, (
            f"at {size['width']}x{size['height']} the board pane is {pane}px across a {column}px "
            f"column and scrolls sideways")


def test_one_long_control_does_not_widen_the_page(start_app, open_page):
    """§3.8 "Scrolling": a control whose own content is wider than the window narrows rather than
    pushing the page across. The engine picker in server mode is the one that can reach this — a
    catalog label is whatever the deployment named its engine (§6.3). The protocol select stands in
    for it here: the catalog picker is `hidden` outside server mode, and the same
    `.engine-line select` rule covers both.

    This is a pair, not two guards: the row is a flex item of the top bar, so `min-width: 0` on the
    row is what lets the row narrow, and only then can `min-width: 0` on the fields narrow the
    control inside it. Measured at this width with this label, neither alone moves the page off
    410px and both together bring it to 320 — so a test that removed one at a time would call both
    of them dead."""
    g = open_page(start_app()).open()
    resized(g, FLOOR)
    g.page.evaluate("""() => {
        const pick = document.querySelector('#protocol');
        const option = document.createElement('option');
        option.textContent = 'handol-mux human policy on a1005 gpu 1 (very long label)';
        pick.appendChild(option);
        pick.value = option.value;
    }""")
    settled(g)

    across, within = g.page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]")
    assert across <= within + 1, (
        f"a long engine label makes the page {across}px across a {within}px window; "
        f"past the edge: {widest_overflow(g)}")
