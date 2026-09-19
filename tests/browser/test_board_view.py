"""The board view and the evaluation (SPEC §3.8 "Evaluation", "Board overlays"; baseline features
B4 candidates and labels, B5 PV preview, B6 heatmap, B7 ownership, B8 Black's view, B31 bar and
counts, B32 move numbers, B33 captures).

Analysis frames are injected through a WebSocket proxy (browser_kit ``proxy_ws``), so each test
knows exactly what the page was given; the real server still sends every ``state``. The page's
drawing is observed through the board canvas's draw record (§3.8 "Test observability").
Analysis is switched on at the server first, so no ``state`` "turns analysis off" (§3.8).
"""

from __future__ import annotations

import re

import pytest

from browser_kit import (ENGINE, analysis_frame, analysis_payload, expect, index_of, move_info,
                         post_sgf)

pytestmark = pytest.mark.browser

SIZE = 19
POINTS = ["K10", "D4", "Q16", "D16", "Q4", "C3", "R17", "C17", "R3", "F3", "O17", "F17", "O3",
          "J3", "L17", "H3", "M17", "G3", "N17", "E3", "P17", "E17", "P3", "G17", "N3"]


def infos(count: int, **overrides) -> list[dict]:
    """``count`` candidates on distinct points, most visits first."""
    return [move_info(POINTS[i], **{"visits": 1000 - 10 * i, "prior": 0.2 - 0.005 * i,
                                    "order": i, **overrides}) for i in range(count)]


def flat_policy(value: float = 0.002) -> list[float]:
    return [value] * (SIZE * SIZE) + [0.001]


def board(start_app, open_page, *extra, engine=None, connect=False):
    """A page on a fresh 19x19 board with analysis on at the server, proxied for injection."""
    app = start_app(engine, connect, *extra)
    g = open_page(app)
    g.proxy_ws()
    g.open()
    g.act({"type": "analysis", "enabled": True})
    return g


def show(g, payload: dict, *, cursor: int | None = None) -> None:
    g.inject(analysis_frame(g.state(), payload, cursor=cursor))


def settle(g) -> None:
    """Wait until every injected frame so far has been handled: inject a log line and wait for
    it in the console log (frames are handled in order)."""
    marker = f"settle-{g.mark()}"
    g.inject({"type": "log", "line": {"direction": "recv", "text": marker, "at": 0}})
    expect(g.page.locator("#log")).to_contain_text(marker)


def cells(g, row: int = 0) -> list[str]:
    return g.page.locator("#candidates tr").nth(row).locator("td").all_inner_texts()


def header(g) -> list[str]:
    return [text.strip() for text in
            g.page.locator("table.candidates thead th").all_inner_texts()]


# -- candidates (B4) -----------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["winrate", "visits", "prior", "score"])
def test_the_label_select_sets_the_label_mode_drawn(start_app, open_page, mode):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(5)))
    g.page.locator("#label-mode").select_option(mode)
    g.expect_dataset("labelMode", mode)


def test_the_label_mode_starts_as_winrate(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(5)))
    g.expect_dataset("labelMode", "winrate")


def test_every_candidate_is_drawn_when_there_are_few(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(5)))
    g.expect_dataset("candidates", "5")


def test_at_most_twelve_candidates_are_drawn(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(15)))
    g.expect_dataset("candidates", "12")


def test_a_pass_candidate_among_the_first_twelve_draws_no_circle(start_app, open_page):
    g = board(start_app, open_page)
    moves = infos(15)
    moves[3] = move_info("pass", visits=900, order=3, pv=["pass"])
    show(g, analysis_payload(SIZE, moves))
    g.expect_dataset("candidates", "11")


def test_the_candidate_table_shows_the_first_ten(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(15)))
    expect(g.page.locator("#candidates tr")).to_have_count(10)


def test_the_candidate_table_has_the_katago_columns(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    expect(g.page.locator("#candidates tr")).to_have_count(3)
    assert header(g) == [g.t(k) for k in ("col.move", "col.win", "col.score", "col.visits",
                                          "col.policy")]


def test_the_first_row_is_marked_as_the_best(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    expect(g.page.locator("#candidates tr").first).to_have_class(re.compile(r"\bbest\b"))


def test_a_row_shows_move_win_for_the_side_searched_signed_score_visits_and_policy(
        start_app, open_page):
    """B8: the engine searched for White, so a Black winrate of 0.3 reads 70.0% in the table."""
    g = board(start_app, open_page)
    first = move_info("K10", winrate=0.3, score=2.5, visits=1234, prior=0.123)
    show(g, analysis_payload(SIZE, [first], current="W"))
    expect(g.page.locator("#candidates tr")).to_have_count(1)
    assert [c.strip() for c in cells(g)] == ["K10", "70.0%", "+2.5", "1.2k", "12.3%"]


def test_a_null_value_in_the_table_reads_as_a_dash(start_app, open_page):
    g = board(start_app, open_page)
    first = move_info("K10", winrate=None, score=None, prior=None)
    show(g, analysis_payload(SIZE, [first]))
    expect(g.page.locator("#candidates tr")).to_have_count(1)
    assert [c.strip() for c in cells(g)][1:3] == ["-", "-"]


def test_the_win_column_uses_to_play_when_the_engine_names_no_player(start_app, open_page):
    """§0: the side searched for is ``currentPlayer``, else the frame's ``toPlay`` (Black on a
    fresh board)."""
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", winrate=0.3)], current=""))
    expect(g.page.locator("#candidates tr")).to_have_count(1)
    assert cells(g)[1].strip() == "30.0%"


def test_a_handol_analysis_has_the_move_win_score_prob_columns(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3, visits=0), source="handol", policy=flat_policy()))
    expect(g.page.locator("#candidates tr")).to_have_count(3)
    assert header(g) == [g.t(k) for k in ("col.move", "col.win", "col.score", "col.prob")]


def test_clicking_a_row_plays_its_move_for_the_side_to_move(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    since = g.mark()
    g.page.locator("#candidates tr").nth(1).click()
    assert g.wait_sent("play", since) == {"type": "play", "color": "black", "vertex": "D4"}


# -- PV preview (B5) ----------------------------------------------------------------------------------
PV = ["K10", "Q16", "pass", "D4", "K10", "C3"]


def test_hovering_a_row_previews_its_pv(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("preview", "K10")


def test_a_preview_draws_each_point_once_and_skips_passes(start_app, open_page):
    """K10, Q16, (pass), D4, (K10 again), C3: four stones."""
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("previewStones", "4")


def test_a_preview_draws_at_most_twenty_moves(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=POINTS[:25])]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("previewStones", "20")


def test_a_preview_hides_the_candidate_circles(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("candidates", "0")


def test_leaving_the_row_clears_the_preview(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("preview", "K10")
    g.page.locator("#move-counter").hover()
    g.expect_dataset("preview", "")


def test_the_candidates_come_back_after_the_preview(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.page.locator("#candidates tr").first.hover()
    g.expect_dataset("candidates", "0")
    g.page.locator("#move-counter").hover()
    g.expect_dataset("candidates", "4")


def test_hovering_a_candidate_on_the_board_previews_its_pv(start_app, open_page):
    """The canvas centre is K10, the tengen of the 19x19 board."""
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.expect_dataset("candidates", "4")
    g.page.mouse.move(*g.center())
    g.expect_dataset("preview", "K10")


def test_leaving_the_candidate_on_the_board_clears_the_preview(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, [move_info("K10", pv=PV)] + infos(4)[1:]))
    g.expect_dataset("candidates", "4")
    g.page.mouse.move(*g.center())
    g.expect_dataset("preview", "K10")
    g.page.locator("#move-counter").hover()
    g.expect_dataset("preview", "")


# -- heatmap (B6) --------------------------------------------------------------------------------------
def test_the_heatmap_is_off_until_ticked(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3), policy=flat_policy()))
    g.expect_dataset("candidates", "3")
    g.expect_dataset("heatmap", "off")


def test_ticking_the_heatmap_draws_the_policy(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3), policy=flat_policy()))
    g.page.locator("#show-policy").check()
    g.expect_dataset("heatmap", "policy")


def test_an_empty_policy_draws_no_heatmap(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3), policy=None))
    g.page.locator("#show-policy").check()
    g.expect_dataset("candidates", "3")
    g.expect_dataset("heatmap", "off")


def test_the_heatmap_checkbox_is_not_sent(start_app, open_page):
    g = board(start_app, open_page)
    since = g.mark()
    g.page.locator("#show-policy").check()
    g.page.locator("#show-numbers").check()
    g.page.locator("#label-mode").select_option("visits")
    g.fence()
    assert g.sent(since) == []


# -- ownership (B7) ------------------------------------------------------------------------------------
def ownership() -> list[float]:
    values = [0.0] * (SIZE * SIZE)
    values[index_of("D4", SIZE)] = 0.9
    values[index_of("Q16", SIZE)] = -0.8
    values[index_of("K10", SIZE)] = 0.05     # below 0.06: not drawn
    return values


def test_ticking_ownership_sends_include_ownership(start_app, open_page):
    g = board(start_app, open_page)
    since = g.mark()
    g.page.locator("#show-ownership").check()
    assert g.wait_sent("engine_params", since)["includeOwnership"] is True


def test_the_ownership_tick_follows_the_server(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "engine_params", "includeOwnership": True})
    expect(g.page.locator("#show-ownership")).to_be_checked()


def test_ownership_is_drawn_when_ticked_and_present(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "engine_params", "includeOwnership": True})
    show(g, analysis_payload(SIZE, infos(3), ownership=ownership()))
    g.expect_dataset("ownership", "on")


def test_ownership_is_not_drawn_when_the_analysis_has_none(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "engine_params", "includeOwnership": True})
    show(g, analysis_payload(SIZE, infos(3), ownership=[]))
    g.expect_dataset("candidates", "3")
    g.expect_dataset("ownership", "off")


def test_ownership_is_not_drawn_while_unticked(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3), ownership=ownership()))
    g.expect_dataset("candidates", "3")
    g.expect_dataset("ownership", "off")


# -- evaluation (B8, B31) --------------------------------------------------------------------------------
def test_the_winrate_bar_reads_blacks_winrate(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), winrate=0.3, current="W"))
    expect(g.page.locator("#winbar-label")).to_have_text(
        re.compile(r"^\s*black\s+30\.0%\s*/\s*white\s+70\.0%\s*$"))


def test_the_winrate_bar_black_part_is_blacks_winrate(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), winrate=0.3, current="W"))
    expect(g.page.locator("#winbar-label")).to_contain_text("30.0%")
    width = g.page.evaluate("() => parseFloat(document.getElementById('winbar-black').style.width)")
    assert width == pytest.approx(30.0)


@pytest.mark.parametrize("lead, text", [(-2.5, "W+2.5"), (3.04, "B+3.0"), (0.0, "B+0.0")])
def test_the_score_lead_reads_b_or_w_plus(start_app, open_page, lead, text):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), score=lead))
    expect(g.page.locator("#score-lead")).to_have_text(text)


@pytest.mark.parametrize("visits, text", [(950, "950"), (1234, "1.2k"), (15000, "15k"),
                                          (1_300_000, "1.3M")])
def test_the_visit_count_is_abbreviated(start_app, open_page, visits, text):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), visits=visits))
    expect(g.page.locator("#visit-count")).to_have_text(g.t("visits.count", {"n": text}))


def test_the_bar_reads_no_engine_while_disconnected(start_app, open_page):
    g = board(start_app, open_page)
    expect(g.page.locator("#winbar-label")).to_have_text(g.t("noEngine"))


def test_the_bar_reads_dashes_while_connected_without_an_analysis(start_engine, start_app,
                                                                   open_page):
    app = start_app(start_engine("gtp"), True)
    g = open_page(app).open()
    expect(g.page.locator("#engine-state")).not_to_have_text(g.t("disconnected"), timeout=ENGINE)
    expect(g.page.locator("#winbar-label")).to_have_text("--")


def test_an_analysis_for_another_cursor_is_ignored(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), winrate=0.3))
    expect(g.page.locator("#winbar-label")).to_contain_text("30.0%")
    show(g, analysis_payload(SIZE, infos(2), winrate=0.9), cursor=5)
    settle(g)
    expect(g.page.locator("#winbar-label")).to_contain_text("30.0%")


def test_the_last_applied_analysis_is_exposed_for_tests(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), winrate=0.3))
    settle(g)
    assert g.page.evaluate("() => window.__lastAnalysis && window.__lastAnalysis.rootInfo.winrate") \
        == 0.3


def test_an_ignored_analysis_is_not_the_last_applied_one(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(2), winrate=0.3))
    show(g, analysis_payload(SIZE, infos(2), winrate=0.9), cursor=5)
    settle(g)
    assert g.page.evaluate("() => window.__lastAnalysis && window.__lastAnalysis.rootInfo.winrate") \
        == 0.3


def test_a_state_that_changes_the_position_clears_the_candidates(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    g.expect_dataset("candidates", "3")
    g.act({"type": "play", "color": "black", "vertex": "A1"})
    g.expect_dataset("candidates", "0")


def test_a_state_that_changes_the_position_clears_the_table(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    expect(g.page.locator("#candidates tr")).to_have_count(3)
    g.act({"type": "play", "color": "black", "vertex": "A1"})
    expect(g.page.locator("#candidates tr")).to_have_count(0)


def test_a_state_that_turns_analysis_off_clears_the_candidates(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    g.expect_dataset("candidates", "3")
    g.act({"type": "analysis", "enabled": False})
    g.expect_dataset("candidates", "0")


def test_a_state_that_keeps_the_position_keeps_the_candidates(start_app, open_page):
    g = board(start_app, open_page)
    show(g, analysis_payload(SIZE, infos(3)))
    g.expect_dataset("candidates", "3")
    draws = g.draws()
    g.fence()
    g.expect_dataset("candidates", "3")
    assert g.draws() >= draws


# -- move numbers (B32) and captures (B33) -----------------------------------------------------------------
def test_move_numbers_are_off_until_ticked(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    g.expect_dataset("numbers", "off")


def test_ticking_move_numbers_draws_them(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    g.page.locator("#show-numbers").check()
    g.expect_dataset("numbers", "on")


def test_unticking_move_numbers_removes_them(start_app, open_page):
    g = board(start_app, open_page)
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    g.page.locator("#show-numbers").check()
    g.expect_dataset("numbers", "on")
    g.page.locator("#show-numbers").uncheck()
    g.expect_dataset("numbers", "off")


def test_the_capture_counts_show_each_colours_captures(start_app, open_page):
    """Black captures the white stone at B1 (Black B2, White B1, Black C1... then A1)."""
    from session_helpers import sgf_of

    app = start_app()
    post_sgf(app.url, sgf_of(9, "B2", "B1", "C1", "E5", "A1"))
    g = open_page(app).open()
    expect(g.page.locator("#captures-black")).to_have_text("1")


def test_the_side_to_move_is_shown(start_app, open_page):
    from session_helpers import sgf_of

    app = start_app()
    post_sgf(app.url, sgf_of(9, "E5"))
    g = open_page(app).open()
    expect(g.page.locator("#to-play")).to_have_text(g.t("white"))


def test_the_move_counter_reads_cursor_over_move_count(start_app, open_page):
    from session_helpers import sgf_of

    app = start_app()
    post_sgf(app.url, sgf_of(9, "E5", "C3", "G7"))
    g = open_page(app).open()
    g.act({"type": "navigate", "index": 1})
    expect(g.page.locator("#move-counter")).to_have_text("1 / 3")


# -- compare views (B22) -------------------------------------------------------------------------------------
def compare_payload() -> dict:
    """A handol analysis whose B distribution moves weight from D4 to Q16 and C3."""
    a = [0.0] * (SIZE * SIZE + 1)
    b = [0.0] * (SIZE * SIZE + 1)
    a[index_of("D4", SIZE)], b[index_of("D4", SIZE)] = 0.6, 0.3
    a[index_of("Q16", SIZE)], b[index_of("Q16", SIZE)] = 0.3, 0.4
    a[index_of("C3", SIZE)], b[index_of("C3", SIZE)] = 0.1, 0.3
    a[index_of("K10", SIZE)] = b[index_of("K10", SIZE)] = 0.0004   # |diff| < 0.001: no candidate
    primary = [move_info("D4", visits=0, prior=0.6, winrate=0.55),
               move_info("Q16", visits=0, prior=0.3, winrate=0.5),
               move_info("C3", visits=0, prior=0.1, winrate=0.45)]
    second = [move_info("Q16", visits=0, prior=0.4, winrate=None),
              move_info("D4", visits=0, prior=0.3, winrate=None),
              move_info("C3", visits=0, prior=0.3, winrate=None)]
    return analysis_payload(SIZE, primary, policy=a, source="handol",
                            compare={"policy": b, "moveInfos": second,
                                     "probabilities": [0.4, 0.3, 0.3]})


def comparing(start_app, open_page):
    """A page showing the human panel with Compare two tuples ticked."""
    g = board(start_app, open_page)
    g.page.locator("#protocol").select_option("handol")
    since = g.mark()
    g.page.locator("#compare-on").check()
    g.wait_sent("human_params", since)
    return g


def test_ticking_compare_selects_the_difference_view(start_app, open_page):
    g = comparing(start_app, open_page)
    expect(g.page.locator("#compare-view")).to_have_value("diff")


def test_the_difference_view_draws_the_diff_heatmap(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    g.page.locator("#show-policy").check()
    g.expect_dataset("heatmap", "diff")


def test_the_difference_view_draws_the_moves_that_differ(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    g.expect_dataset("candidates", "3")


def test_the_a_view_draws_the_primary_policy(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    g.page.locator("#show-policy").check()
    g.page.locator("#compare-view").select_option("A")
    g.expect_dataset("heatmap", "policy")


def test_the_b_view_draws_the_compare_candidates(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    g.page.locator("#compare-view").select_option("B")
    g.expect_dataset("candidates", "3")


def test_a_comparison_has_the_move_win_score_a_b_delta_columns(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    expect(g.page.locator("#candidates tr").first).to_be_visible()
    assert header(g) == [g.t(k) for k in ("col.move", "col.win", "col.score", "col.a", "col.b",
                                          "col.delta")]


def test_the_delta_column_is_b_minus_a_signed(start_app, open_page):
    g = comparing(start_app, open_page)
    show(g, compare_payload())
    g.page.locator("#compare-view").select_option("A")
    expect(g.page.locator("#candidates tr").first).to_be_visible()
    assert [c.strip() for c in cells(g)][0:1] + [c.strip() for c in cells(g)][3:6] == \
        ["D4", "60.0%", "30.0%", "-30.0%"]


def test_the_human_panel_switches_to_policy_labels_and_the_heatmap_the_first_time(
        start_app, open_page):
    g = board(start_app, open_page)
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#label-mode")).to_have_value("prior")


def test_the_human_panel_ticks_the_heatmap_the_first_time(start_app, open_page):
    g = board(start_app, open_page)
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#show-policy")).to_be_checked()


def test_the_human_panel_switches_the_label_mode_only_the_first_time(start_app, open_page):
    g = board(start_app, open_page)
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#label-mode")).to_have_value("prior")
    g.page.locator("#label-mode").select_option("visits")
    g.page.locator("#protocol").select_option("gtp")
    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#label-mode")).to_have_value("visits")
