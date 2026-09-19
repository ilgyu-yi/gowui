"""The handol-mux human policy panel (SPEC §3.8 "Human policy panel", §2.5, §8.5; baseline
features B3 move styles, B19 profile help, B20 knobs / raw values / JSON, B21 presets, B22 compare,
B23 eval visits, B24 the side-to-move refusal).

The knobs are ``#tuple-knobs input[type=range]`` in §3.8 order (strength, locality, variety,
tail cut); the raw fields are ``#tuple-fields input`` in §2.5 key order.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from browser_kit import ENGINE, expect, post_sgf

pytestmark = pytest.mark.browser

KEYS = ["lambda_utility", "trust_mu", "fill_kappa", "min_p", "distance_slope", "distance_floor",
        "distance_peak", "temperature"]
KNOBS = ["strength", "locality", "variety", "tail"]
LAMBDA = {"lambda_utility": 0.1, "trust_mu": 0.05, "fill_kappa": 1}


def panel(g):
    return g.page.locator("details:has(#human-profile)")


def knob(g, name: str):
    return g.page.locator("#tuple-knobs input[type=range]").nth(KNOBS.index(name))


def field(g, key: str):
    return g.page.locator("#tuple-fields input").nth(KEYS.index(key))


def open_raw_values(g) -> None:
    raw = g.page.locator("details:has(#human-policy)").last   # the innermost: Raw values
    if raw.get_attribute("open") is None:
        raw.locator("> summary").click()
    expect(g.page.locator("#human-policy")).to_be_visible()


def slide(g, name: str, value: int, *, commit: bool) -> None:
    """Move a knob as a drag does (``input``), and release it (``change``) when ``commit``."""
    knob(g, name).evaluate(
        "(el, [v, commit]) => { el.value = String(v); el.dispatchEvent(new Event('input',"
        " {bubbles: true})); if (commit) el.dispatchEvent(new Event('change', {bubbles: true})); }",
        [value, commit])


def type_json(g, text: str) -> None:
    open_raw_values(g)
    g.page.locator("#human-policy").fill(text)
    g.page.locator("#human-policy").dispatch_event("change")


def form_handol(start_app, open_page, **page_options):
    """The page with the engine form switched to handol (no engine needed for the panel)."""
    g = open_page(start_app(), **page_options).open()
    g.page.locator("#protocol").select_option("handol")
    expect(panel(g)).to_be_visible()
    return g


def connected(start_engine, start_app, open_page):
    g = open_page(start_app(start_engine("handol"), True)).open()
    expect(g.page.locator("#connect")).to_have_text(g.t("disconnect"), timeout=ENGINE)
    return g


# -- when the panel shows (§3.8) ----------------------------------------------------------------------------
def test_the_panel_is_hidden_for_a_gtp_form(start_app, open_page):
    g = open_page(start_app()).open()
    expect(panel(g)).to_be_hidden()


def test_the_panel_shows_when_the_form_protocol_is_handol(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#protocol").select_option("handol")
    expect(panel(g)).to_be_visible()


def test_the_panel_shows_while_a_handol_engine_is_connected(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    expect(panel(g)).to_be_visible()


def test_the_move_style_row_shows_with_the_panel(start_app, open_page):
    g = form_handol(start_app, open_page)
    expect(g.page.locator("#black-style")).to_be_visible()


def test_the_move_style_row_is_hidden_without_handol(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#black-style")).to_be_hidden()


@pytest.mark.parametrize("select, key", [("black-style", "blackStyle"),
                                         ("white-style", "whiteStyle")])
def test_a_move_style_select_sends_players(start_app, open_page, select, key):
    g = form_handol(start_app, open_page)
    since = g.mark()
    g.page.locator(f"#{select}").select_option("katago")
    assert g.wait_sent("players", since)[key] == "katago"


# -- profile (B19) --------------------------------------------------------------------------------------------
def test_the_profile_field_shows_the_boards_profile(start_app, open_page):
    g = form_handol(start_app, open_page)
    expect(g.page.locator("#human-profile")).to_have_value("preaz_1d")


def test_the_profile_field_suggests_the_known_profiles(start_app, open_page):
    g = form_handol(start_app, open_page)
    listed = g.page.locator("#human-profile").evaluate(
        "(el) => el.list ? Array.from(el.list.options).map((o) => o.value) : []")
    assert {"preaz_1d", "rank_5k", "proyear_2020"} <= set(listed)


def test_changing_the_profile_sends_human_params(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    g.page.locator("#human-profile").fill("rank_5k")
    g.page.locator("#human-profile").press("Tab")
    assert g.wait_sent("human_params", since)["profile"] == "rank_5k"


def test_hovering_the_question_mark_explains_the_profile(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#human-profile").fill("rank_5k")
    g.page.locator("#profile-help").hover()
    expect(g.page.locator(".help-pop")).to_be_visible()
    expect(g.page.locator(".help-pop")).to_contain_text("rank_5k")


def test_focusing_the_question_mark_explains_the_profile(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#profile-help").focus()
    expect(g.page.locator(".help-pop")).to_be_visible()


def test_an_unknown_profile_is_explained_as_unknown(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#human-profile").fill("mystery_x")
    g.page.locator("#profile-help").hover()
    expect(g.page.locator(".help-pop")).to_contain_text(
        g.t("profile.desc.unknown", {"name": "mystery_x"}))


def test_changing_winrate_visits_sends_eval_visits(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    g.page.locator("#eval-visits").fill("300")
    g.page.locator("#eval-visits").press("Tab")
    assert g.wait_sent("human_params", since)["evalVisits"] == 300


# -- knobs, raw values and JSON (B20) ---------------------------------------------------------------------------
def test_a_knob_fills_the_json(start_app, open_page):
    g = form_handol(start_app, open_page)
    slide(g, "locality", 50, commit=False)
    open_raw_values(g)
    assert "distance_slope" in json.loads(g.page.locator("#human-policy").input_value())


def test_a_knob_fills_its_raw_field(start_app, open_page):
    g = form_handol(start_app, open_page)
    slide(g, "locality", 50, commit=False)
    expect(field(g, "distance_slope")).not_to_have_value("")


def test_the_json_fills_the_raw_fields(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.05}')
    expect(field(g, "min_p")).to_have_value("0.05")


def test_the_json_moves_the_knob(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.05}')
    expect(knob(g, "tail")).not_to_have_value("0")


def test_a_raw_field_fills_the_json(start_app, open_page):
    g = form_handol(start_app, open_page)
    open_raw_values(g)
    field(g, "temperature").fill("1.5")
    field(g, "temperature").dispatch_event("change")
    expect(g.page.locator("#human-policy")).to_have_value(re.compile(r'"temperature":\s*1\.5'))


def test_a_committed_change_sends_the_tuple_with_no_compare(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    type_json(g, '{"min_p": 0.05}')
    frame = g.wait_sent("human_params", since)
    assert (frame["policy"], frame["compare"]) == ({"min_p": 0.05}, None)


def test_a_released_knob_sends_the_tuple(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    slide(g, "locality", 50, commit=True)
    assert g.wait_sent("human_params", since)["policy"]["distance_slope"] > 0


PAUSED = datetime(2030, 1, 1, 12, 0, 0)


def paused_handol(start_app, open_page):
    g = open_page(start_app())
    g.page.clock.install(time=PAUSED)
    g.page.clock.pause_at(PAUSED + timedelta(seconds=1))
    g.open()
    g.page.locator("#protocol").select_option("handol")
    expect(panel(g)).to_be_visible()
    return g


def test_a_dragged_knob_waits_300_ms_before_sending(start_app, open_page):
    g = paused_handol(start_app, open_page)
    since = g.mark()
    slide(g, "locality", 50, commit=False)
    g.page.clock.run_for(290)
    g.fence()
    assert g.sent(since, "human_params") == []


def test_a_dragged_knob_sends_300_ms_after_the_last_change(start_app, open_page):
    g = paused_handol(start_app, open_page)
    since = g.mark()
    slide(g, "locality", 40, commit=False)
    g.page.clock.run_for(200)
    slide(g, "locality", 50, commit=False)
    g.page.clock.run_for(310)
    frame = g.wait_sent("human_params", since)
    assert len(g.sent(since, "human_params")) == 1 and frame["policy"]["distance_slope"] > 0


def test_a_dragged_knob_sends_nothing_while_apply_while_dragging_is_off(start_app, open_page):
    g = paused_handol(start_app, open_page)
    g.page.locator("#live-apply").uncheck()
    since = g.mark()
    slide(g, "locality", 50, commit=False)
    g.page.clock.run_for(1_000)
    g.fence()
    assert g.sent(since, "human_params") == []


def test_a_refused_tuple_is_shown_under_the_panel(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 2}')
    expect(g.page.locator("#tuple-problem")).to_be_visible()


def test_a_refused_tuple_is_not_sent(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    type_json(g, '{"min_p": 2}')
    expect(g.page.locator("#tuple-problem")).to_be_visible()
    g.fence()
    assert g.sent(since, "human_params") == []


def test_json_that_is_not_an_object_is_refused(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    type_json(g, "[1, 2]")
    expect(g.page.locator("#tuple-problem")).to_be_visible()
    g.fence()
    assert g.sent(since, "human_params") == []


def test_lambda_is_refused_with_one_visit(start_app, open_page):
    """The Visits field takes part in the check (§2.5: lambda_utility needs a search)."""
    g = form_handol(start_app, open_page)
    g.page.locator("#max-visits").fill("1")
    g.page.locator("#max-visits").press("Tab")
    expect(g.page.locator("#max-visits")).to_have_value("1")
    since = g.mark()
    type_json(g, json.dumps(LAMBDA))
    expect(g.page.locator("#tuple-problem")).to_be_visible()
    g.fence()
    assert g.sent(since, "human_params") == []


def test_lambda_is_accepted_with_a_search(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    type_json(g, json.dumps(LAMBDA))
    assert g.wait_sent("human_params", since)["policy"] == LAMBDA


def test_the_problem_text_is_hidden_once_the_tuple_is_valid(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 2}')
    expect(g.page.locator("#tuple-problem")).to_be_visible()
    type_json(g, '{"min_p": 0.2}')
    expect(g.page.locator("#tuple-problem")).to_be_hidden()


def test_hovering_a_knob_shows_its_help_card(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#tuple-knobs .knob").nth(1).hover()
    expect(g.page.locator(".hover-card")).to_be_visible()
    expect(g.page.locator(".hover-card")).to_contain_text(g.t("knob.locality"))


def test_hovering_a_raw_field_shows_its_help_card(start_app, open_page):
    g = form_handol(start_app, open_page)
    open_raw_values(g)
    g.page.locator("#tuple-fields .tuple-field").nth(KEYS.index("min_p")).hover()
    expect(g.page.locator(".hover-card")).to_contain_text("min_p")


# -- presets (B21, §8.5) -----------------------------------------------------------------------------------
def stored_presets(g):
    return g.page.evaluate("() => JSON.parse(localStorage.getItem('gowui.userPresets') || 'null')")


def save_preset(g, name: str) -> None:
    g.page.once("dialog", lambda dialog: dialog.accept(name))
    g.page.locator("#preset-save").click()
    g.until("(n) => (localStorage.getItem('gowui.userPresets') || '')"
            ".includes(JSON.stringify(n))", arg=name)


def test_a_built_in_preset_sends_its_tuple(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    g.page.locator("#human-preset").select_option(label=g.t("preset.minP"))
    assert g.wait_sent("human_params", since)["policy"] == {"min_p": 0.05}


def test_save_as_stores_the_tuple_under_the_chosen_name(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    assert stored_presets(g) == [{"name": "mine", "tuple": {"min_p": 0.1}}]


def test_a_saved_preset_is_offered_in_the_select(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    labels = g.page.locator("#human-preset option").all_inner_texts()
    assert any("mine" in label for label in labels)


def test_saving_over_an_existing_name_asks_first(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    type_json(g, '{"min_p": 0.2}')
    dialogs = []

    def answer(dialog):
        dialogs.append(dialog.type)
        dialog.accept("mine") if dialog.type == "prompt" else dialog.accept()

    g.page.on("dialog", answer)
    g.page.locator("#preset-save").click()
    g.until("() => (localStorage.getItem('gowui.userPresets') || '')"
            ".includes('0.2')")
    assert (dialogs, stored_presets(g)) == (["prompt", "confirm"],
                                           [{"name": "mine", "tuple": {"min_p": 0.2}}])


def test_delete_is_disabled_for_a_built_in_preset(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#human-preset").select_option(label=g.t("preset.minP"))
    expect(g.page.locator("#preset-delete")).to_be_disabled()


def test_delete_asks_and_removes_the_own_preset(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    expect(g.page.locator("#preset-delete")).to_be_enabled()
    dialogs = []
    g.page.once("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
    g.page.locator("#preset-delete").click()
    g.until("() => localStorage.getItem('gowui.userPresets') === '[]'")
    assert dialogs == ["confirm"]


def test_declining_the_delete_keeps_the_preset(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    dialogs = []
    g.page.once("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.dismiss()))
    g.page.locator("#preset-delete").click()
    g.fence()
    assert (dialogs, stored_presets(g)) == (["confirm"],
                                           [{"name": "mine", "tuple": {"min_p": 0.1}}])


def test_export_downloads_the_presets_file(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    with g.page.expect_download() as download:
        g.page.locator("#preset-export").click()
    data = json.loads(open(download.value.path(), encoding="utf-8").read())
    assert (download.value.suggested_filename, data) == (
        "gowui-presets.json", {"gowuiPresets": 1,
                               "presets": [{"name": "mine", "tuple": {"min_p": 0.1}}]})


def import_file(g, content: str | bytes) -> None:
    data = content.encode("utf-8") if isinstance(content, str) else content
    g.page.locator("#preset-file").set_input_files(
        {"name": "presets.json", "mimeType": "application/json", "buffer": data})


IMPORTED = {"gowuiPresets": 1, "presets": [
    {"name": "a", "tuple": {"min_p": 0.1}},
    {"name": "", "tuple": {}},                   # no name: skipped
    {"name": "bad", "tuple": {"foo": 1}},        # refused by §2.5: skipped
    {"name": "lam", "tuple": LAMBDA},            # checked as if searching: kept
    {"tuple": {"min_p": 0.3}},                   # no name: skipped
]}


def test_import_keeps_the_valid_named_presets(start_app, open_page):
    g = form_handol(start_app, open_page)
    import_file(g, json.dumps(IMPORTED))
    g.until("() => localStorage.getItem('gowui.userPresets') !== null")
    assert [p["name"] for p in stored_presets(g)] == ["a", "lam"]


def test_import_reports_what_it_added_and_skipped(start_app, open_page):
    g = form_handol(start_app, open_page)
    import_file(g, json.dumps(IMPORTED))
    expect(g.page.locator("#status")).to_have_text(
        g.t("preset.imported", {"added": 2, "skipped": 3}))


def test_import_accepts_a_bare_list(start_app, open_page):
    g = form_handol(start_app, open_page)
    import_file(g, json.dumps([{"name": "x", "tuple": {"temperature": 1.5}}]))
    g.until("() => localStorage.getItem('gowui.userPresets') !== null")
    assert stored_presets(g) == [{"name": "x", "tuple": {"temperature": 1.5}}]


def test_import_reports_a_file_it_cannot_read(start_app, open_page):
    g = form_handol(start_app, open_page)
    import_file(g, b"\x00 not json")
    expect(g.page.locator("#status")).to_have_text(g.t("preset.importBad"))


def test_an_unreadable_stored_preset_list_counts_as_empty(start_app, open_page):
    g = form_handol(start_app, open_page, storage={"gowui.userPresets": "{not json"})
    expect(g.page.locator("#preset-delete")).to_be_disabled()
    assert g.problems() == []


def test_the_page_stores_nothing_but_the_two_keys(start_app, open_page):
    g = form_handol(start_app, open_page)
    type_json(g, '{"min_p": 0.1}')
    save_preset(g, "mine")
    g.page.locator("#lang").select_option("ko")
    g.page.locator("#compare-on").check()
    keys = g.page.evaluate("() => Object.keys(localStorage)")
    assert set(keys) <= {"gowui.lang", "gowui.userPresets"}


# -- compare (B22) ---------------------------------------------------------------------------------------------
def test_ticking_compare_sends_a_compare_tuple(start_app, open_page):
    g = form_handol(start_app, open_page)
    since = g.mark()
    g.page.locator("#compare-on").check()
    assert isinstance(g.wait_sent("human_params", since)["compare"], dict)


def test_ticking_compare_shows_the_a_b_tabs_and_the_show_select(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#compare-on").check()
    expect(g.page.locator("#tuple-tabs")).to_be_visible()
    expect(g.page.locator("#compare-view")).to_be_visible()


def test_unticking_compare_sends_a_null_compare(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#compare-on").check()
    expect(g.page.locator("#tuple-tabs")).to_be_visible()
    since = g.mark()
    g.page.locator("#compare-on").uncheck()
    assert g.wait_sent("human_params", since)["compare"] is None


def test_the_a_tab_edits_the_primary_tuple(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#compare-on").check()
    g.page.locator("#tuple-tabs [data-slot='A']").click()
    since = g.mark()
    type_json(g, '{"temperature": 0.5}')
    frame = g.wait_sent("human_params", since)
    assert (frame["policy"], frame["compare"]) == ({"temperature": 0.5}, {})


def test_the_b_tab_edits_the_compare_tuple(start_app, open_page):
    g = form_handol(start_app, open_page)
    g.page.locator("#compare-on").check()
    g.page.locator("#tuple-tabs [data-slot='B']").click()
    since = g.mark()
    type_json(g, '{"temperature": 0.5}')
    frame = g.wait_sent("human_params", since)
    assert (frame["policy"], frame["compare"]) == ({}, {"temperature": 0.5})


def test_a_handol_comparison_fills_the_a_b_delta_table(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#compare-on").check()
    g.page.locator("#analysis-on").check()
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    heads = [h.strip() for h in g.page.locator("table.candidates thead th").all_inner_texts()]
    assert heads == [g.t(k) for k in ("col.move", "col.win", "col.score", "col.a", "col.b",
                                      "col.delta")]


def test_a_handol_analysis_draws_its_distribution(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    g.expect_dataset("heatmap", "policy", timeout=ENGINE)


# -- eval visits (B23) and the side-to-move refusal (B24) -------------------------------------------------------
def test_eval_visits_bring_a_winrate_to_the_bar(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    expect(g.page.locator("#winbar-label")).to_contain_text("%", timeout=ENGINE)


def test_the_side_to_move_refusal_is_shown_as_status_text(start_engine, start_app, open_page):
    """White to move on an empty board: handol-mux would put Black on move (§2.5)."""
    engine = start_engine("handol")
    app = start_app(engine, True)
    post_sgf(app.url, "(;GM[1]FF[4]SZ[19]PL[W])")
    g = open_page(app).open()
    expect(g.page.locator("#connect")).to_have_text(g.t("disconnect"), timeout=ENGINE)
    g.page.locator("#analysis-on").check()
    expect(g.page.locator("#status")).to_contain_text("play a move first", timeout=ENGINE)
