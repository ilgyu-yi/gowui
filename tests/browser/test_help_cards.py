"""The `?` cards beside the controls, in a browser (SPEC §3.8 "Where explanation lives"; issue
#67).

§3.8: a static card "opens while its `?` is hovered or has focus — a tap focuses it — and closes
when neither holds"; it opens "against the right edge of its row" and is "never wider than that
row"; "hovering one `?` closes the card of a focused one beside it"; the table card
"holds the current mode's columns and no others", and "each column header carries its entry's text
as its `title`, from the same key".

Which cards must exist is read from the source (``board_option_controls``: the handlers that call
``board.setOptions(``, plus the Compare checkbox and the table), so a missing card fails by the
control it belongs to. A card's text is checked only for **not** reading as its own key:
``i18n.t`` falls back to the key, so a line reading ``help.label.winrate`` is what a missing string
looks like on the page (§3.8 "Language").
"""

from __future__ import annotations

import pytest

from browser_kit import INIT_SCRIPT, QUICK, Gowui, analysis_frame, analysis_payload, expect, \
    move_info, vertex
from frontend_helpers import board_option_controls

pytestmark = pytest.mark.browser

#: The one scripted `?` (§3.8 "Where explanation lives"): its card follows the typed profile.
SCRIPTED_DOT = "#profile-help"


def with_human_panel(g: Gowui) -> Gowui:
    """Show the human policy panel, where the Compare checkbox and its `?` live: the panel follows
    the engine form's protocol (§3.8 "Human policy panel")."""
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#compare-on")).to_be_visible(timeout=QUICK)
    return g


def expected_dots(g: Gowui) -> dict[str, str]:
    """``{what: aria-describedby}`` for every `?` §3.8 requires, found from the control it
    explains; a control without one maps to ``""``."""
    return g.page.evaluate("""(ids) => {
        const out = {};
        for (const id of ids) {
            const label = document.getElementById(id).closest('label');
            const dot = label && label.nextElementSibling;
            out[id] = dot && dot.classList.contains('help-dot')
                ? dot.getAttribute('aria-describedby') || '' : '';
        }
        const table = document.querySelector('.help-card [data-col]');
        const card = table && table.closest('.help-card');
        const dot = card && card.previousElementSibling;
        out['candidate table'] = dot && dot.classList.contains('help-dot')
            ? dot.getAttribute('aria-describedby') || '' : '';
        return out;
    }""", [*board_option_controls(), "compare-on"])


def static_dots(g: Gowui) -> list[str]:
    """The ``aria-describedby`` of every `?` on the page but the scripted one, in page order, once
    each `?` §3.8 requires is known to be there."""
    missing = sorted(what for what, card in expected_dots(g).items() if not card)
    assert missing == [], f"no ? card beside: {missing}"
    return g.page.evaluate("""(scripted) => [...document.querySelectorAll('.help-dot')]
        .filter((d) => !d.matches(scripted)).map((d) => d.getAttribute('aria-describedby'))""",
                           SCRIPTED_DOT)


def dot(g: Gowui, card: str):
    return g.page.locator(f'.help-dot[aria-describedby="{card}"]')


def card(g: Gowui, card_id: str):
    return g.page.locator(f"#{card_id}")


def blur(g: Gowui) -> None:
    g.page.evaluate("() => document.activeElement && document.activeElement.blur()")


def away(g: Gowui) -> None:
    """The pointer off every control: the top-left corner is the page title."""
    g.page.mouse.move(1, 1)


# -- opening and closing ----------------------------------------------------------------------------
def test_every_card_starts_closed(start_app, open_page):
    g = with_human_panel(open_page(start_app()).open())
    shown = [c for c in static_dots(g) if card(g, c).is_visible()]
    assert shown == []


def test_focus_opens_a_card_and_blur_closes_it(start_app, open_page):
    """§3.8: a card opens while its `?` has focus — how a keyboard reaches it."""
    g = with_human_panel(open_page(start_app()).open())
    for card_id in static_dots(g):
        dot(g, card_id).focus()
        expect(card(g, card_id)).to_be_visible(timeout=QUICK)
        blur(g)
        expect(card(g, card_id)).to_be_hidden(timeout=QUICK)


def test_hover_opens_a_card_and_leaving_closes_it(start_app, open_page):
    g = with_human_panel(open_page(start_app()).open())
    for card_id in static_dots(g):
        dot(g, card_id).hover()
        expect(card(g, card_id)).to_be_visible(timeout=QUICK)
        away(g)
        expect(card(g, card_id)).to_be_hidden(timeout=QUICK)


def test_a_tap_opens_a_card(start_app, browser):
    """§3.8: "a tap focuses it" — touch has no hover to leave, so a touch reader is served by the
    focus rule alone."""
    app = start_app()
    context = browser.new_context(has_touch=True, locale="en-US",
                                  viewport={"width": 1400, "height": 1000})
    try:
        context.add_init_script(INIT_SCRIPT)
        g = with_human_panel(Gowui(context.new_page(), app.url).open())
        for card_id in static_dots(g):
            dot(g, card_id).tap()
            expect(card(g, card_id)).to_be_visible(timeout=QUICK)
            g.page.locator("h1").tap()
            expect(card(g, card_id)).to_be_hidden(timeout=QUICK)
    finally:
        context.close()


def test_hovering_one_dot_closes_the_focused_card_beside_it(start_app, open_page):
    """§3.8: "Two cards in one row open in the same place; hovering one `?` closes the card of a
    focused one beside it" — otherwise one card is drawn over the other."""
    g = with_human_panel(open_page(start_app()).open())
    rows = g.page.evaluate("""() => {
        const rows = new Map();
        for (const d of document.querySelectorAll('.help-dot[aria-describedby]')) {
            const row = d.parentElement;
            rows.set(row, [...(rows.get(row) || []), d.getAttribute('aria-describedby')]);
        }
        return [...rows.values()].filter((ids) => ids.length > 1);
    }""")
    assert rows, "no row holds two ? cards, so nothing can overlap"
    for first, second, *_ in rows:
        dot(g, first).focus()
        dot(g, second).hover()
        expect(card(g, second)).to_be_visible(timeout=QUICK)
        expect(card(g, first)).to_be_hidden(timeout=QUICK)
        away(g)
        expect(card(g, first)).to_be_visible(timeout=QUICK)
        blur(g)


# -- what they say ------------------------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["en", "ko"])
def test_no_card_line_reads_as_its_own_key(start_app, open_page, lang):
    g = with_human_panel(open_page(start_app()).open())
    g.page.locator("#lang").select_option(lang)
    expect(g.page.locator("html")).to_have_attribute("lang", lang, timeout=QUICK)
    lines = g.page.evaluate("""(ids) => ids.flatMap((id) =>
        [...document.getElementById(id).querySelectorAll('[data-i18n]')]
            .map((n) => [n.dataset.i18n, n.textContent.trim()]))""", static_dots(g))
    assert lines, "the cards carry no data-i18n text"
    assert [key for key, text in lines if not text or text == key] == []


# -- the table card, per mode -------------------------------------------------------------------------
def table_card_id(g: Gowui) -> str:
    card_id = expected_dots(g)["candidate table"]
    assert card_id, "the candidate table has no ? card"
    return card_id


def table_reading(g: Gowui, card_id: str) -> dict:
    """With the table card open: the header keys, the columns of the entries on show, and per
    header its ``title`` against its entry's text."""
    dot(g, card_id).focus()
    expect(card(g, card_id)).to_be_visible(timeout=QUICK)
    reading = g.page.evaluate("""(id) => {
        const heads = [...document.querySelectorAll('table.candidates thead th')];
        const entries = [...document.getElementById(id).querySelectorAll('[data-col]')];
        const shown = entries.filter((e) => e.getClientRects().length > 0);
        const textOf = (key) => {
            const e = entries.find((x) => x.dataset.col.split(' ').includes(key));
            const t = e && e.querySelector('.help-text');
            return t ? t.textContent.trim() : null;
        };
        return {
            heads: heads.map((th) => th.dataset.i18n),
            shown: shown.flatMap((e) => e.dataset.col.split(' ')),
            titles: heads.map((th) => [th.dataset.i18n, th.title, textOf(th.dataset.i18n)]),
        };
    }""", card_id)
    blur(g)
    return reading


def test_the_table_card_follows_the_mode(start_app, open_page):
    """§3.8: the table card "holds the current mode's columns and no others", and each header's
    ``title`` is its entry's text — in the default, the handol-mux and the comparing modes."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    card_id = table_card_id(g)
    size = g.state()["game"]["size"]
    infos = [move_info(vertex(x, 0, size)) for x in range(3)]
    policy = [0.0] * (size * size + 1)
    other = [0.0] * (size * size + 1)
    policy[0], other[1] = 0.6, 0.6

    modes = [
        ("default", analysis_payload(size, infos), 6),
        ("handol", analysis_payload(size, infos, source="handol", policy=policy), 6),
        ("handol-compare", analysis_payload(size, infos, source="handol", policy=policy,
                                            compare={"policy": other, "moveInfos": infos}), 8),
    ]
    for mode, payload, columns in modes:
        g.inject(analysis_frame(g.state(), payload))
        expect(g.page.locator("table.candidates thead th")).to_have_count(columns, timeout=QUICK)
        reading = table_reading(g, card_id)
        heads = reading["heads"]
        shown_keys = set(reading["shown"])
        assert set(heads) <= shown_keys, f"{mode}: columns without a shown entry: " \
            f"{sorted(set(heads) - shown_keys)}"
        assert_no_foreign_entry(g, card_id, heads, mode)
        wrong = [(key, title, text) for key, title, text in reading["titles"]
                 if not title or title != text]
        assert wrong == [], f"{mode}: a header's title is not its entry's text: {wrong}"


def test_a_header_title_follows_a_language_switch(start_app, open_page):
    """§3.8: a header's ``title`` is its entry's text "from the same key", so a language switch
    (``i18n.apply``) re-renders it with the card rather than leaving the language it was built
    in."""
    g = open_page(start_app()).open()
    card_id = table_card_id(g)
    before = table_reading(g, card_id)["titles"]
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("html")).to_have_attribute("lang", "ko", timeout=QUICK)
    after = table_reading(g, card_id)["titles"]
    wrong = [(key, title, text) for key, title, text in after if not title or title != text]
    assert wrong == [], f"after the switch a header's title is not its entry's text: {wrong}"
    assert [t for _, t, _ in after] != [t for _, t, _ in before], "no title changed language"


def assert_no_foreign_entry(g: Gowui, card_id: str, heads: list[str], mode: str) -> None:
    """No entry is on show whose columns the mode does not have."""
    dot(g, card_id).focus()
    foreign = g.page.evaluate("""([id, heads]) => [...document.getElementById(id)
        .querySelectorAll('[data-col]')].filter((e) => e.getClientRects().length > 0)
        .map((e) => e.dataset.col).filter((cols) => !cols.split(' ').some((k) => heads.includes(k)))""",
                              [card_id, heads])
    blur(g)
    assert foreign == [], f"{mode}: entries on show for columns the table does not have: {foreign}"


# -- the card and the page's scrolling (§3.8 "Scrolling") ---------------------------------------------
def test_an_open_card_scrolls_nothing_sideways(start_app, open_page):
    """§3.8 "Where explanation lives" and "The page scrolls down, never across": with each card
    open, the document gains no horizontal scrolling at the 320px floor and the card keeps its left
    edge in the window; at 1400px the side panel gains no sideways scrollbar."""
    g = with_human_panel(open_page(start_app()).open())
    found = []
    for size in ({"width": 1400, "height": 1000}, {"width": 320, "height": 700}):
        g.page.set_viewport_size(size)
        g.until("(w) => window.innerWidth === w", size["width"])
        for card_id in static_dots(g):
            dot(g, card_id).focus()
            expect(card(g, card_id)).to_be_visible(timeout=QUICK)
            fit = g.page.evaluate("""(id) => {
                const doc = document.documentElement, side = document.querySelector('.side');
                const box = document.getElementById(id).getBoundingClientRect();
                return {page: [doc.scrollWidth, doc.clientWidth],
                        side: [side.scrollWidth, side.clientWidth],
                        left: box.left, right: box.right};
            }""", card_id)
            at = f"{card_id} at {size['width']}"
            if fit["page"][0] > fit["page"][1] + 1:
                found.append((at, "page", fit))
            if size["width"] > 980 and fit["side"][0] > fit["side"][1] + 1:
                found.append((at, "side", fit))
            if fit["left"] < 0:
                found.append((at, "left edge", fit))
            blur(g)
    assert found == [], f"an open card scrolls something sideways: {found}"


# -- the profile card is left as it was (issue #67 "What not to touch") --------------------------------
def test_the_profile_card_still_opens_on_hover_and_focus(start_app, open_page):
    g = with_human_panel(open_page(start_app()).open())
    pop = g.page.locator(".help-pop")
    g.page.locator(SCRIPTED_DOT).hover()
    expect(pop).to_be_visible(timeout=QUICK)
    expect(pop).to_contain_text("preaz_1d")
    away(g)
    expect(pop).to_be_hidden(timeout=QUICK)
    g.page.locator(SCRIPTED_DOT).focus()
    expect(pop).to_be_visible(timeout=QUICK)
    blur(g)
    expect(pop).to_be_hidden(timeout=QUICK)
