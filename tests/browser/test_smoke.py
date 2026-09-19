"""Browser smoke run (issue #8 AC4; SPEC §3.8): against the local app and the fake engine, the page
shows the board, a candidate overlay once analysis is on, and a second board after Duplicate —
with no console error and no CSP violation (§7.5). Screenshots go to ``test-artifacts/``.
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


def test_duplicate_shows_a_second_board(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#board-duplicate").click()
    expect(g.page.locator("#board-list .thumb")).to_have_count(2)


def test_the_duplicate_is_the_active_board(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#board-duplicate").click()
    expect(g.page.locator("#board-list .thumb").nth(1)).to_have_class(re.compile(r"\bactive\b"))


def test_the_smoke_flow_end_to_end(start_engine, start_app, open_page):
    """The whole AC4 flow in one page, with the screenshots the pull request shows."""
    g = connected(start_engine, start_app, open_page)
    g.screenshot("smoke-board")
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    g.page.locator("#board-duplicate").click()
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

