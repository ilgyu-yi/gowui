"""What scrolls, and what does not, at each width (SPEC §3.8 "Scrolling", "Board strip"; issue
#45).

Three checks, one per rule the §3.8 "Scrolling" paragraph adds: above 980px the document does not
scroll and each column scrolls inside itself with the board standing still; a window too short for
the side panel still reaches every control, by a column's scrolling and never by the page's; at
980px and below the page scrolls as one column while the board strip scrolls sideways within
itself.

The viewport here is deliberately shorter than the shared context's 1400x1000, because the rule is
only visible in a window the content does not fit. Every read after a viewport change waits on a
layout predicate — the board's resize handler runs on its own frame, so a box read before it is the
old one.
"""

from __future__ import annotations

import pytest

from browser_kit import QUICK, Gowui, expect

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


def duplicate_to(g: Gowui, count: int) -> None:
    """Fill the strip with ``count`` boards through the page's own "+ Duplicate" button."""
    tiles = g.page.locator("#board-list .thumb")
    for wanted in range(tiles.count() + 1, count + 1):
        g.page.locator("#board-duplicate").click()
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
    duplicate_to(g, 6)

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


def test_the_narrow_layout_scrolls_as_one_column(start_app, open_page):
    """§3.8 "Scrolling", narrow: at 980px and below the layout is one column and the page scrolls
    as a whole, while the board strip lies across the top and scrolls sideways within itself
    (§3.8 "Board strip")."""
    g = open_page(start_app()).open()
    resized(g, NARROW)
    duplicate_to(g, 8)

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
