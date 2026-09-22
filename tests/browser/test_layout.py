"""What scrolls, and what does not, at each width (SPEC §3.8 "Scrolling", "Board strip"; issue
#45).

The checks, one per rule the §3.8 "Scrolling" paragraph carries: above 980px the document does not
scroll and each column scrolls inside itself with the board standing still; a window too short for
the side panel still reaches every control, by a column's scrolling and never by the page's; at
980px and below the page scrolls as one column while the board strip scrolls sideways within
itself; and from 320px up the page scrolls down and never across (issue #48).

More come from what a column's width costs its contents (§3.8 "Candidate table", "Candidate
readout"; issue #44): the table cuts no number in any mode, at any supported width, or with a
value no column could have been sized for; its surplus width is its own box's sideways scrolling
and never the panel's or the page's; and the readout loses whole fields off its end rather than
shrinking them into each other. All of it is measured per box — a cell's or a field's own
`scrollWidth` against its `clientWidth` — because a screenshot cannot tell `100.0%` from `100…` at
a glance and a green page cannot tell either.

The last three read the two surfaces **together**, and over the window's height as well as its
width. Either surface alone can be green while the field is unreadable: the readout's room is
`min(78vh, 900px)`, so a wide but short window loses fields a width sweep never sees, and the
table's box can clip a whole column at its edge with nothing to say so where the platform draws
overlay scrollbars. Where the Value is reachable is therefore a fact about the window, not about a
surface, and that is what is swept.

The table's checks sweep 1400 / 500 / 400 / 360 / 320px instead of measuring one width. A
guarantee §3.8 states unconditionally has to be tested unconditionally: the columns-share-a-width
layout these replace passed at the one width its shares were measured at, cut seven of eight at
360px, and cut two on Linux font metrics that it fit on macOS.

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
#: An ordinary laptop window: wide, and short enough that the readout's `min(78vh, 900px)` is what
#: runs out rather than the width (§3.8 "Candidate readout").
WIDE_SHORT = {"width": 1400, "height": 900}
#: Wide and tall enough for every readout field — the control the height legs are read against.
ROOMY = {"width": 1400, "height": 1000}


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


def fitted(g: Gowui) -> None:
    """Wait out the page's own fit pass. What the readout can hold and which of the table box's
    edges clip are read from the layout, so both run once per animation frame rather than once per
    analysis frame (§3.8); a surface read in the same frame as a resize or a render is the old
    one. Two frames: the pass is scheduled in one and runs in the next."""
    g.page.evaluate("() => new Promise((done) => requestAnimationFrame("
                    "() => requestAnimationFrame(done)))")


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


def comparing_at_its_widest(g: Gowui, **wide) -> None:
    """Inject a comparing analysis whose every column carries the widest value it can hold
    (§3.8 "Candidate table"): a full winrate, a three-figure loss, a seven-figure visit count, a
    policy that moves nearly the whole way between the two tuples, and a signed utility. A ``wide``
    field replaces one of them with a value no column could have been sized for.

    Narrower data would not test the layout: candidates with two-digit visits and no utility fit a
    table that takes its columns from its contents, so the table would sit inside the panel however
    the columns were shared out and both checks below would pass on a page that cuts numbers."""
    size = g.state()["game"]["size"]
    infos = [{**move_info(vertex(x, 0, size), winrate=1.0, score=-123.4, visits=1234567),
              "utility": -1.23, "utilityLcb": -1.23, **wide} for x in range(10)]
    a = [0.0] * (size * size + 1)
    b = [0.0] * (size * size + 1)
    # The two ends of Δ, on the first two candidates: all of A's weight on one and all of B's on
    # the other, so one row reads +99.7% and the other -99.7% while A and B both reach 100.0%.
    a[0], b[0] = 1.0, 0.003
    a[1], b[1] = 0.003, 1.0
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, infos, source="handol", policy=a, compare={"policy": b, "moveInfos": infos})))
    expect(g.page.locator("table.candidates thead th")).to_have_count(8, timeout=QUICK)
    expect(g.page.locator("#candidates tr")).to_have_count(10, timeout=QUICK)


def default_at_its_widest(g: Gowui, **wide) -> None:
    """The six-column table with the widest value every column can hold, the same bar
    ``comparing_at_its_widest`` sets for the eight. A ``wide`` field replaces one of them with a
    value no column could have been sized for (§2.2 takes whatever the engine sends)."""
    size = g.state()["game"]["size"]
    infos = [{**move_info(vertex(x, 0, size), winrate=1.0, score=-123.4, visits=1234567,
                          prior=1.0), "utility": -1.23, "utilityLcb": -1.23, **wide}
             for x in range(10)]
    g.inject(analysis_frame(g.state(), analysis_payload(size, infos)))
    expect(g.page.locator("table.candidates thead th")).to_have_count(6, timeout=QUICK)


def column_fit(g: Gowui) -> list[list]:
    """Per column: its header, the widest text in it, and that cell's ``scrollWidth`` against its
    ``clientWidth``. A cell whose content is wider than its box is a cut one, which no reading of
    the rendered text can see — `100…` is a string the page never composed."""
    return g.page.evaluate("""() => {
        const head = [...document.querySelectorAll('table.candidates thead th')];
        return head.map((th, i) => {
            const cells = [th, ...document.querySelectorAll(
                'table.candidates tbody tr td:nth-child(' + (i + 1) + ')')];
            const worst = cells.reduce((a, c) =>
                (c.scrollWidth - c.clientWidth) > (a.scrollWidth - a.clientWidth) ? c : a);
            return [th.textContent, worst.textContent, worst.scrollWidth, worst.clientWidth];
        });
    }""")


def cut_columns(g: Gowui) -> list[str]:
    """The columns whose widest cell does not fit its box, named with the numbers that say so."""
    return [f"{head.strip()} {text.strip()!r} {scroll}>{client}"
            for head, text, scroll, client in column_fit(g) if scroll > client]


def sideways(g: Gowui) -> dict:
    """What scrolls across at the current width: the document, the side panel, and the candidate
    table's own box. The first two must not (§3.8 "The page scrolls down, never across"); the
    third is the one box allowed to, and is where the surplus width goes."""
    return g.page.evaluate("""() => {
        const across = (el) => el && [el.scrollWidth, el.clientWidth];
        return {page: across(document.documentElement),
                side: across(document.querySelector('.side')),
                box: across(document.querySelector('.candidates-box'))};
    }""")


#: The widths §3.8 "The page scrolls down, never across" supports, from a window wider than the
#: layout needs down to its 320px floor. Swept in one page rather than parametrised: the rule is
#: about what a *width* costs the table, so the first width that breaks it is the report.
CANDIDATE_WIDTHS = (1400, 500, 400, 360, 320)


def candidates_swept(g: Gowui, fill) -> list[tuple]:
    """Apply ``fill`` at each of ``CANDIDATE_WIDTHS`` and collect (width, cut columns, boxes)."""
    swept = []
    for width in CANDIDATE_WIDTHS:
        resized(g, {"width": width, "height": 700})
        fill(g)
        swept.append((width, cut_columns(g), sideways(g)))
    return swept


def test_no_comparing_cell_cuts_a_number(start_app, open_page):
    """§3.8 "Candidate table": no numeric cell is ever cut, at every width §3.8 supports and with
    the widest value each column can hold. A column narrower than its number renders `100…`, which
    still reads as a number and is off by an order of magnitude.

    Swept rather than measured at one width because the guarantee §3.8 makes is unconditional: a
    table whose columns share a fixed width passes at the width its shares were measured at and
    cuts seven of its eight columns at 360px, and a test at one width calls that a pass."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()

    swept = candidates_swept(g, comparing_at_its_widest)
    cut = [(width, columns) for width, columns, _ in swept if columns]
    assert cut == [], f"the comparing table cuts a number at {len(cut)} of its widths: {cut}"


def test_the_six_column_modes_do_not_cut_a_number_either(start_app, open_page):
    """§3.8 "Candidate table": the same bar in the six-column modes, over the same widths — the
    guarantee is per mode, so the comparing rules cannot be bought at the other modes' expense."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()

    swept = candidates_swept(g, default_at_its_widest)
    cut = [(width, columns) for width, columns, _ in swept if columns]
    assert cut == [], f"the default table cuts a number at {len(cut)} of its widths: {cut}"


def test_the_table_is_at_the_body_size_in_every_mode(start_app, open_page):
    """§3.8 "Candidate table": the table is set at the body size in every mode; nothing is bought
    by shrinking the type. The eight comparing columns used to be paid for with a step down, which
    left the table the smallest text on the page and still did not make the guarantee hold."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    resized(g, SHORT)

    sizes = {}
    for mode, fill in (("default", default_at_its_widest), ("comparing", comparing_at_its_widest)):
        fill(g)
        sizes[mode] = g.page.evaluate(
            "() => [getComputedStyle(document.querySelector('table.candidates')).fontSize,"
            " getComputedStyle(document.body).fontSize]")
    assert [size[0] == size[1] for size in sizes.values()] == [True, True], (
        f"the table is not at the body size in every mode: {sizes}")


def test_the_table_scrolls_sideways_and_neither_the_panel_nor_the_page_does(start_app, open_page):
    """§3.8 "The page scrolls down, never across" with "Candidate table": the table's surplus
    width is reached by its **own box's** sideways scrolling, the standing the board strip has.
    Neither the side panel — a box that scrolls vertically treats a horizontal overflow as
    scrollable too — nor the document may gain sideways scrolling at any supported width.

    The box is also asserted to actually scroll somewhere in the sweep: a table that fits every
    width would pass the first two on a page that had quietly gone back to cutting."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()

    swept = candidates_swept(g, comparing_at_its_widest)
    across = [(width, boxes) for width, _, boxes in swept
              if boxes["page"][0] > boxes["page"][1] or boxes["side"][0] > boxes["side"][1]]
    assert across == [], (
        f"the page or the side panel scrolls sideways at {len(across)} widths: {across}")
    boxed = [width for width, _, boxes in swept if boxes["box"] is None]
    assert boxed == [], ("the table has no box of its own to scroll, so its surplus width has "
                        f"nowhere to go but the panel's or the page's: {boxed}")
    scrolled = [width for width, _, boxes in swept if boxes["box"][0] > boxes["box"][1]]
    assert scrolled, ("the table's box never scrolled in the sweep, so nothing here shows where "
                      f"its surplus width goes: {[boxes for _, _, boxes in swept]}")


@pytest.mark.parametrize("field, value", [
    ("visits", 1_234_500_000_000),   # Visits: `1234500.0M`
    ("utility", -12345.6789),         # Value:  `-12345.68`
    ("scoreLead", -12345.6),          # Score:  `-12345.6`
])
def test_an_over_wide_value_is_not_cut_and_costs_no_other_column(start_app, open_page,
                                                                 field, value):
    """§3.8 "Candidate table": no numeric cell is **ever** cut — not only the values §3.8 names.
    §2.2 takes whatever the engine sends, so a value no column could have been sized for has to
    be shown whole too, and its width may not be taken from the columns beside it.

    Three columns, not just Visits, and in the comparing table where the room is tightest: an
    over-wide Value or Score is the same case, and the predicate that once let Visits through by
    name would have let those two be cut in silence."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    resized(g, SHORT)
    comparing_at_its_widest(g, **{field: value})

    assert cut_columns(g) == [], (
        f"an over-wide {field} left {len(cut_columns(g))} columns cut: {cut_columns(g)}")
    boxes = sideways(g)
    assert boxes["side"][0] == boxes["side"][1] and boxes["page"][0] == boxes["page"][1], (
        f"an over-wide {field} pushed the panel or the page sideways: {boxes}")


def readout_fit(g: Gowui) -> dict:
    """What the readout line is holding at the current window: every field in order, the ones it
    still shows, the ones it dropped, and any shown field the line's own edge crosses."""
    return g.page.evaluate("""() => {
        const line = document.querySelector('#candidate-readout');
        const edge = line.getBoundingClientRect().right;
        const all = [...line.querySelectorAll('.field')];
        const text = (f) => f.textContent.trim();
        return {
            fields: all.map(text),
            kept: all.filter((f) => !f.hidden).map(text),
            lost: all.filter((f) => f.hidden).map(text),
            cut: all.filter((f) => !f.hidden
                                   && f.getBoundingClientRect().right > edge + 0.5)
                    .map((f) => text(f) + ' past ' + Math.round(edge)),
            squeezed: all.filter((f) => f.scrollWidth > f.clientWidth)
                         .map((f) => text(f) + ' ' + f.scrollWidth + '>' + f.clientWidth),
            room: Math.round(line.clientWidth),
        };
    }""")


def test_the_readout_loses_whole_fields_off_its_end(start_app, open_page):
    """§3.8 "Candidate readout": a window too small for every field loses whole fields off the
    **end** — the Value and its bound first, since the fields are in the table's column order.

    Two things it must not do. It must not shrink every field to fit: the fields are flex items,
    and a flex item squeezed below its content does not cut its text, it lets the label and the
    number paint over the field beside it, so the line would lose nothing cleanly and become
    unreadable everywhere at once. And it must not cut the one field its own edge falls in: a
    number shown short of its last digits still reads as a number and is off by an order of
    magnitude, which is why §3.8 "Candidate table" cuts no cell either.

    The shapes sweep the **height** as well as the width, because the line's room is
    `min(78vh, 900px)` — the board's width, and below about 926px of viewport height the height is
    what governs it. 1400x900 and 1100x560 are wide windows that lose fields; a width-only sweep
    (480x700, 360x700) says nothing about them. 1400x1000 is the control: the same width, losing
    nothing, which is what makes the loss at 1400x900 a fact about the height."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()

    resized(g, ROOMY)
    comparing_at_its_widest(g)
    fitted(g)
    roomy = readout_fit(g)
    assert roomy["lost"] == [], (
        f"at 1400x1000 the line has {roomy['room']}px and already drops {roomy['lost']}, so what "
        "a shorter window costs it cannot be read off these shapes")

    for size in (WIDE_SHORT, SHORT, {"width": 480, "height": 700}, {"width": 360, "height": 700}):
        resized(g, size)
        comparing_at_its_widest(g)
        fitted(g)
        fit = readout_fit(g)
        at = f"at {size['width']}x{size['height']}"
        assert fit["squeezed"] == [], (
            f"{at} the readout squeezes {len(fit['squeezed'])} fields below their own text, "
            f"which paints them over each other: {fit['squeezed']}")
        assert fit["cut"] == [], (
            f"{at} the line cuts where its edge falls instead of dropping whole fields, so "
            f"{len(fit['cut'])} of them are shown short of their last digits: {fit['cut']}")
        assert fit["lost"], (f"{at} the line has {fit['room']}px and loses nothing of "
                             f"{fit['fields']}: nothing here is being measured")
        assert fit["kept"] + fit["lost"] == fit["fields"], (
            f"{at} what the line lost is {fit['lost']}, which is not the end of {fit['fields']}")
        assert fit["lost"][-1].startswith(g.t("col.value")), (
            f"{at} the last field off the end is {fit['lost'][-1]!r}, not the Value")


#: The window shapes the Value has to be reachable at (§3.8 "Where the Value is reachable"): wide
#: and tall, the wide-and-short shapes where the readout's `min(78vh, 900px)` runs out, the narrow
#: end of the wide layout, and the narrow layout down to the 320px floor. Heights as well as
#: widths: the shape that lost the Value altogether was a wide one.
VALUE_SHAPES = ((1400, 1000), (1400, 926), (1400, 900), (1400, 700), (1100, 560), (981, 700),
                (500, 700), (400, 700), (360, 700), (320, 700))


def where_the_value_is(g: Gowui, label: str) -> dict:
    """Where the Value can be read at the current window: on the readout line, in the table
    without scrolling, in the table after its box is scrolled — and what the box says about the
    edge it clips at. Every leg is measured against a box, never against a screenshot: a column
    clipped at the box's edge and a column the table does not have look the same."""
    return g.page.evaluate("""(label) => {
        const line = document.querySelector('#candidate-readout');
        const edge = line.getBoundingClientRect().right;
        const field = [...line.querySelectorAll('.field')]
            .find((f) => f.textContent.trim().startsWith(label));
        const onLine = !!field && !field.hidden
                       && field.getBoundingClientRect().right <= edge + 0.5;
        const box = document.querySelector('.candidates-box');
        const columns = document.querySelectorAll('table.candidates thead th').length;
        const cell = document.querySelector(
            'table.candidates tbody tr td:nth-child(' + columns + ')');
        const inside = () => {
            const b = box.getBoundingClientRect();
            const c = cell.getBoundingClientRect();
            return c.left >= b.left - 0.5 && c.right <= b.right + 0.5;
        };
        const style = getComputedStyle(box);
        const marked = box.dataset.more || '';
        const faded = style.maskImage !== 'none';
        const unscrolled = box.scrollLeft < 0.5 && inside();
        box.scrollLeft = box.scrollWidth;
        // Whether the person can scroll the box, not merely whether this test can: a box with
        // `overflow: hidden` still takes a scrollLeft from script, and reading the Value out of it
        // that way would be a reading nobody but the test can make.
        const scrolled = ['auto', 'scroll'].includes(style.overflowX) && inside();
        box.scrollLeft = 0;
        return {onLine: onLine, unscrolled: unscrolled, scrolled: scrolled, marked: marked,
                faded: faded, needs: Math.round(box.scrollWidth),
                room: Math.round(box.clientWidth),
                track: box.offsetHeight - box.clientHeight};
    }""", label)


def test_the_value_is_reachable_at_every_window(start_app, open_page):
    """§3.8 "Candidate readout" ("Where the Value is reachable") with "Candidate table": at every
    window shape the Value is on the readout line, or in the table without scrolling, or in the
    table by its box's own sideways scrolling **with the clipped edge saying so**.

    Nowhere is the case this test exists for, and it was a common window: at 1400x900 the line had
    702px for 722px of fields, the eight comparing columns wanted 401px in a 358px box with the
    Value at 1373-1416 against an edge of 1373, and the platform reserved no scrollbar track
    (`offsetHeight - clientHeight` is 0 where overlay scrollbars are drawn) to say the table had
    more. The field this PR exists to surface was on neither surface and nothing said so.

    Swept over height as well as width because the readout's room is `min(78vh, 900px)`: a sweep
    of widths at one height cannot see the shape that loses it. The two surfaces are read
    together — where the Value is reachable is a fact about the window, not about a surface."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    label = g.t("col.value")

    swept = []
    for width, height in VALUE_SHAPES:
        resized(g, {"width": width, "height": height})
        comparing_at_its_widest(g)
        fitted(g)
        swept.append((f"{width}x{height}", where_the_value_is(g, label)))

    nowhere = [(at, seen) for at, seen in swept
               if not (seen["onLine"] or seen["unscrolled"] or seen["scrolled"])]
    assert nowhere == [], f"the Value is nowhere at {len(nowhere)} of the shapes: {nowhere}"
    silent = [(at, seen) for at, seen in swept
              if not (seen["onLine"] or seen["unscrolled"]) and not seen["marked"]]
    assert silent == [], (
        f"at {len(silent)} shapes the Value is only reachable by scrolling the table's box and "
        f"nothing says the box has more: {silent}")
    # Both halves of the sweep have to be exercised, or the two assertions above pass on a page
    # that shows the Value nowhere but happens to mark every edge, or on one that never scrolls.
    assert any(seen["onLine"] for _, seen in swept), (
        f"the Value is on the readout line at none of the shapes: {swept}")
    assert any(not seen["onLine"] and seen["unscrolled"] for _, seen in swept), (
        f"no shape reads the Value out of the table beside the board: {swept}")

    # The unscrolled reading is bought with the side panel's own width, and nothing above pins it:
    # with the clipped edge marked, a page that gave the table back the 358px box it had — where
    # the Value sat entirely past the edge at every wide window — passes every assertion above.
    # What is pinned is the track, not the fit it buys: 424px is a CSS fact on every platform,
    # while how much of the box's 422px survives a classic scrollbar's gutter and a wider font
    # stack is not (§3.8 "Candidate table").
    resized(g, ROOMY)
    track, room = g.page.evaluate("""() => [
        getComputedStyle(document.querySelector('main')).gridTemplateColumns.split(' ').pop(),
        Math.round(document.querySelector('.candidates-box').clientWidth)]""")
    assert track == "424px", (
        f"the wide layout's side panel is {track}, not the 424px the eight comparing columns "
        f"were measured against; the table's box has {room}px of it")


def edge_mark(g: Gowui) -> tuple[str, bool]:
    """The edges the table's box says have content past them, and whether the page fades one."""
    return tuple(g.page.evaluate("""() => {
        const box = document.querySelector('.candidates-box');
        return [box.dataset.more || '', getComputedStyle(box).maskImage !== 'none'];
    }"""))


def scrolled_to(g: Gowui, where: str) -> None:
    """Scroll the table's box to its start, middle or end and wait out the page's fit pass."""
    g.page.evaluate("""(where) => {
        const box = document.querySelector('.candidates-box');
        const most = box.scrollWidth - box.clientWidth;
        box.scrollLeft = where === 'start' ? 0 : (where === 'end' ? most : most / 2);
    }""", where)
    fitted(g)


def test_the_clipped_edge_of_the_table_says_there_is_more(start_app, open_page):
    """§3.8 "Candidate table" ("Where the box does clip, the clipped edge says so"): the box names
    the edges that have content past them and the page fades them, and the mark follows the box's
    own scrolling — the end edge at rest, both part way along, the start edge at the end.

    A reserved scrollbar track cannot carry this and is why it exists: where the platform draws
    overlay scrollbars nothing is reserved at all, so a column clipped at the box's edge looks
    exactly like a column the table does not have. The asserted state records that track width, so
    a platform that does reserve one is visible in the failure rather than assumed away.

    The attribute and the fade are asserted together: an attribute nothing paints tells the reader
    nothing, and a fade nothing drives cannot follow the scrolling. The overflow is forced with a
    Visits value no column could have been sized for (§2.2 takes whatever the engine sends), so
    the box overflows on any font metrics rather than only on the ones this was measured on."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    resized(g, SHORT)
    comparing_at_its_widest(g, visits=1_234_500_000_000)
    fitted(g)

    room, track = g.page.evaluate("""() => {
        const box = document.querySelector('.candidates-box');
        return [box.scrollWidth + '>' + box.clientWidth,
                box.offsetHeight - box.clientHeight];
    }""")
    marked = {}
    for where in ("start", "middle", "end"):
        scrolled_to(g, where)
        marked[where] = edge_mark(g)
    assert marked == {"start": ("end", True), "middle": ("both", True), "end": ("start", True)}, (
        f"a box of {room} with a {track}px scrollbar track marks and fades its edges as "
        f"{marked}, not end / both / start")

    # Nothing past either edge: no mark and no fade, so the fade is not simply always there.
    size = g.state()["game"]["size"]
    resized(g, ROOMY)
    g.inject(analysis_frame(g.state(), analysis_payload(size, [move_info(vertex(0, 0, size))])))
    expect(g.page.locator("#candidates tr")).to_have_count(1, timeout=QUICK)
    fitted(g)
    assert edge_mark(g) == ("", False), (
        f"a table with nothing past either edge still marks or fades one: {edge_mark(g)}")


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
