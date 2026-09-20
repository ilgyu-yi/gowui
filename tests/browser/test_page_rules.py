"""Page rules that only a browser can show (SPEC §3.8, §8.5; issue #22).

Each test drives the page's own controls and frames, never its internals: the status line after a
``4403`` close, a rename field while its tile moves, the PV preview of a candidate the board did
not draw, an empty Visits field, and the presets this browser has stored.

The frames the server would not send on cue (a reordered ``state``, an ``analysis``) go in through
``proxy_ws`` / ``inject`` of browser_kit.py.
"""

from __future__ import annotations

import json

import pytest

from browser_kit import QUICK, Gowui, analysis_frame, analysis_payload, expect, move_info, vertex

pytestmark = pytest.mark.browser


def hover_point(g: Gowui, x: int, y: int, size: int) -> None:
    """Rest the pointer on point (``x``, ``y``) counted from the top left (board.js geometry)."""
    box = g.page.locator("#board").bounding_box()
    margin = box["width"] / (size + 1.6)
    cell = (box["width"] - 2 * margin) / (size - 1)
    scale = box["height"] / box["width"]
    g.page.mouse.move(box["x"] + margin + x * cell, box["y"] + (margin + y * cell) * scale)


def after_a_draw(g: Gowui, before: int) -> None:
    """Wait until the board has drawn again, so a record read after it is the new one."""
    g.until("(before) => Number(document.getElementById('board').dataset.draws) > before", before)


# -- the status line (§3.8 "Status line", "Connection") ------------------------------------------
def test_a_refused_close_keeps_saying_why(start_app, open_page, tmp_path):
    """§3.8: a ``4403`` reason stands until the page is reloaded — a later message neither
    replaces it nor starts a timer over it."""
    g = open_page(start_app())
    g.allow_console("WebSocket")
    g.proxy_ws()
    g.open()
    refused = g.t("status.refused")

    g.routes[-1].close(code=4403)
    expect(g.page.locator("#status")).to_have_text(refused, timeout=QUICK)

    # A file over 1 MiB is refused in the page itself (§3.8 "SGF"): a status message that needs
    # no socket, so it arrives while the reason stands.
    big = tmp_path / "big.sgf"
    big.write_text("(;GM[1]FF[4]SZ[19]C[" + "x" * 1_100_000 + "])", encoding="utf-8")
    g.page.locator("#sgf-file").set_input_files(str(big))
    g.page.wait_for_timeout(500)
    assert g.page.locator("#status").text_content() == refused


# -- the board strip (§3.8 "Rename in place") ----------------------------------------------------
def test_a_tile_move_does_not_save_an_open_rename(start_app, open_page):
    """§3.8: only Enter or the user leaving the field saves. A ``state`` whose board order moved
    the tile takes the field's focus away, and that must save nothing."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.act({"type": "board_duplicate"})
    expect(g.page.locator("#board-list .thumb")).to_have_count(2, timeout=QUICK)

    state = g.state()
    ids = [board["id"] for board in state["boards"]]
    tile = g.page.locator(f'#board-list .thumb[data-id="{ids[1]}"]')
    tile.locator(".thumb-edit").click()
    field = tile.locator(".thumb-rename")
    field.fill("half typed")

    since = g.mark()
    g.inject({**state, "boards": list(reversed(state["boards"]))})
    g.until("(id) => document.querySelectorAll('#board-list .thumb')[0].dataset.id === id",
            str(ids[1]))

    g.fence()
    assert g.sent(since, "board_rename") == []
    assert field.input_value() == "half typed"
    assert g.page.evaluate("() => document.activeElement.className") == "thumb-rename"


# -- candidates and the PV preview (§3.8 "Board overlays") ---------------------------------------
def test_only_a_drawn_candidate_previews_its_pv(start_app, open_page):
    """§3.8: at most the first 12 ``moveInfos`` are drawn, and a later entry is not on the board,
    so hovering its point previews nothing."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    size = g.state()["game"]["size"]
    infos = [move_info(vertex(x, 0, size), visits=100 - x,
                       pv=[vertex(x, 0, size), vertex(x, 1, size)]) for x in range(14)]
    g.inject(analysis_frame(g.state(), analysis_payload(size, infos)))
    g.expect_dataset("candidates", "12")

    # The 13th candidate: drawn nowhere, so its point previews nothing.
    before = g.draws()
    hover_point(g, 12, 0, size)
    after_a_draw(g, before)
    assert (g.dataset("preview"), g.dataset("candidates")) == ("", "12")

    # The first one, to show the hover itself reaches the board.
    before = g.draws()
    hover_point(g, 0, 0, size)
    after_a_draw(g, before)
    assert (g.dataset("preview"), g.dataset("candidates")) == (vertex(0, 0, size), "0")


# -- engine parameters (§3.8 "Controls") ---------------------------------------------------------
@pytest.mark.parametrize("typed", ["", "0"])
def test_a_visits_field_without_a_usable_number_sends_no_visit_change(start_app, open_page,
                                                                      typed):
    """§3.8: an empty Visits field, or one that is not a whole number of at least 1, sends no
    visit change; the setting keeps its value and the field is filled again from the next
    ``state``."""
    g = open_page(start_app()).open()
    was = g.state()["settings"]["maxVisits"]
    field = g.page.locator("#max-visits")

    field.fill(typed)
    since = g.mark()
    field.dispatch_event("change")
    frame = g.wait_sent("engine_params", since)
    assert "maxVisits" not in frame

    field.blur()
    g.fence()
    assert g.state()["settings"]["maxVisits"] == was
    expect(field).to_have_value(str(was), timeout=QUICK)


def test_a_visits_field_with_a_number_still_sends_it(start_app, open_page):
    g = open_page(start_app()).open()
    field = g.page.locator("#max-visits")
    field.fill("700")
    since = g.mark()
    field.dispatch_event("change")
    assert g.wait_sent("engine_params", since)["maxVisits"] == 700
    field.blur()
    g.fence()
    assert g.state()["settings"]["maxVisits"] == 700


# -- stored presets (§8.5) -----------------------------------------------------------------------
def test_a_stored_preset_the_tuple_rules_refuse_is_dropped(start_app, open_page):
    """§8.5: an entry without a name, or with a tuple §2.5 refuses, is dropped when the list is
    read — the menu offers only the presets that can be sent."""
    stored = [
        {"name": "keeps", "tuple": {"min_p": 0.1}},
        {"name": "lambda", "tuple": {"lambda_utility": 0.5, "trust_mu": 1, "fill_kappa": 0}},
        {"name": "out of range", "tuple": {"min_p": 2}},
        {"name": "unknown key", "tuple": {"nonsense": 1}},
        {"name": "not a number", "tuple": {"temperature": "warm"}},
        {"name": "  ", "tuple": {}},
    ]
    g = open_page(start_app(), storage={"gowui.userPresets": json.dumps(stored)}).open()
    values = g.page.eval_on_selector_all(
        "#human-preset option", "options => options.map((option) => option.value)")
    assert [v for v in values if v.startswith("user:")] == ["user:keeps", "user:lambda"]
