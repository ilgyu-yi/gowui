"""What a newcomer can find, in a browser (SPEC §3.8 "Board strip", "Engine picker", "Human policy
panel"; issue #67).

A tile's tuple line is there only while its board has a human setting of its own; a tile's ✎, ⧉
and × show on keyboard focus as on hover; the tuple hover card opens on focus as on hover; and the
Engine `?` goes with the protocol select when a catalog hides it.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import ENGINE, QUICK, Gowui, expect
from test_help_panel import with_engine_address

pytestmark = pytest.mark.browser


def tuple_line(g: Gowui, board_id: int):
    return g.page.locator(f'#board-list .thumb[data-id="{board_id}"] .thumb-tuple')


def away(g: Gowui) -> None:
    """The pointer off every control: the top-left corner is the page title."""
    g.page.mouse.move(1, 1)


# -- the tuple line follows the board's own settings (§3.8 "Board strip") --------------------------
def test_a_fresh_board_shows_no_tuple_line(start_app, open_page):
    """§3.8: a fresh board (§3.3) has no human setting of its own, so its tile has no tuple line."""
    g = open_page(start_app()).open()
    first = g.state()["activeBoard"]
    second = g.act({"type": "board_new"})["activeBoard"]
    for board_id in (first, second):
        expect(tuple_line(g, board_id)).to_be_attached(timeout=QUICK)
        expect(tuple_line(g, board_id)).to_be_hidden(timeout=QUICK)


@pytest.mark.parametrize("setting, default", [
    ({"profile": "rank_5k"}, {"profile": "preaz_1d"}),
    ({"policy": {"temperature": 1.5}}, {"policy": {}}),
    ({"compare": {}}, {"compare": None}),
], ids=["profile", "tuple", "compare"])
def test_the_tuple_line_shows_while_the_board_has_a_setting_of_its_own(start_app, open_page,
                                                                       setting, default):
    """§3.8: the line shows while the board has a profile other than the default, a non-empty
    tuple or a compare tuple, and goes again when the setting goes back."""
    g = open_page(start_app()).open()
    board = g.state()["activeBoard"]
    line = tuple_line(g, board)
    g.act({"type": "human_params", **setting})
    expect(line).to_be_visible(timeout=QUICK)
    g.act({"type": "human_params", **default})
    expect(line).to_be_hidden(timeout=QUICK)


def test_a_connect_does_not_show_the_tuple_line(start_engine, start_app, open_page):
    """§3.8: the line reads the board's settings, not the engine connection — a handol-mux engine
    connected does not bring it up on a fresh board."""
    g = open_page(start_app(start_engine("handol"), True)).open()
    expect(g.page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    line = tuple_line(g, g.state()["activeBoard"])
    expect(line).to_be_attached(timeout=QUICK)
    expect(line).to_be_hidden(timeout=QUICK)


# -- a tile's buttons on keyboard focus (§3.8 "Board strip") ----------------------------------------
BUTTONS = (".thumb-edit", ".thumb-duplicate", ".thumb-close")


def opacities(g: Gowui, board_id: int) -> dict[str, float]:
    return g.page.evaluate("""([id, selectors]) => {
        const tile = document.querySelector(`#board-list .thumb[data-id="${id}"]`);
        return Object.fromEntries(selectors.map((s) =>
            [s, Number(getComputedStyle(tile.querySelector(s)).opacity)]));
    }""", [board_id, list(BUTTONS)])


def test_a_tiles_buttons_show_while_it_holds_focus(start_app, open_page):
    """§3.8: the ✎, ⧉ and × show while the tile is hovered or holds focus (``:focus-within``)."""
    g = open_page(start_app()).open()
    board = g.state()["activeBoard"]
    away(g)
    tile = g.page.locator(f'#board-list .thumb[data-id="{board}"]')
    expect(tile).to_be_visible(timeout=QUICK)
    assert opacities(g, board) == {s: 0 for s in BUTTONS}, "the buttons show before any focus"
    tile.focus()
    g.until("(id) => document.activeElement.dataset.id === String(id)", board)
    g.page.wait_for_timeout(50)
    assert [s for s, o in opacities(g, board).items() if o == 0] == []


# -- the tuple hover card on focus (§3.8 "Human policy panel") --------------------------------------
def test_a_knob_and_a_field_open_their_card_on_focus(start_app, open_page):
    """§3.8: each knob and field shows its help card while it is hovered or holds focus."""
    g = open_page(start_app()).open()
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#tuple-knobs input").first).to_be_visible(timeout=QUICK)
    g.page.locator("details.advanced > summary").click()
    card = g.page.locator(".hover-card")
    away(g)
    for control in ("#tuple-knobs input", "#tuple-fields input"):
        g.page.locator(control).first.focus()
        expect(card).to_be_visible(timeout=QUICK)
        g.page.evaluate("() => document.activeElement.blur()")
        expect(card).to_be_hidden(timeout=QUICK)


# -- the Engine ? under a catalog (§3.8 "Engine picker") --------------------------------------------
def test_a_catalog_hides_the_engine_card_and_keeps_the_label(start_app, open_page):
    """§3.8: with a catalog the `?` goes with the protocol select it explains, and the "Engine"
    label stays, beside the picker."""
    g = open_page(start_app())
    with_engine_address(g, {"kind": "catalog",
                            "engines": [{"id": "kata", "label": "Fake KataGo", "protocol": "gtp"}]})
    g.open()
    expect(g.page.locator("#engine-pick")).to_be_visible(timeout=QUICK)
    found = g.page.evaluate("""() => {
        const label = document.getElementById('protocol').closest('label');
        const dot = label && label.nextElementSibling;
        return {label: !!label && label.getClientRects().length > 0,
                dot: dot && dot.classList.contains('help-dot')
                    ? dot.getClientRects().length > 0 : null};
    }""")
    assert found == {"label": True, "dot": False}
