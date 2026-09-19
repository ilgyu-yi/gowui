"""The board pane and the game controls (SPEC §3.8 "Controls", "SGF", "Status line"; baseline
features B1 play, B2 engine move, B10 new game, B11 SGF load, B12 SGF save with a chosen name,
B13 moves and navigation, B14 undo / pass / resign, B16 final score).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

import pytest

from browser_kit import ENGINE, expect, post_sgf

pytestmark = pytest.mark.browser

MIB = 1024 * 1024


def game(start_app, open_page, moves: int = 25, *, init: str | None = None, engine=None):
    """A page on a 9x9 game of ``moves`` moves loaded through POST /api/sgf, at the last move."""
    from session_helpers import random_sgf

    app = start_app(engine, engine is not None)
    if moves:
        post_sgf(app.url, random_sgf(moves, 9))
    g = open_page(app, init=init).open()
    expect(g.page.locator("#move-counter")).to_have_text(f"{moves} / {moves}")
    return g


def open_section(g, key: str) -> None:
    """Open the collapsible section whose summary carries ``data-i18n=key``."""
    details = g.page.locator(f"details:has(> summary[data-i18n='{key}'])")
    if details.get_attribute("open") is None:
        details.locator("> summary").click()
    expect(details).to_have_attribute("open", "")


# -- navigation (B13) ----------------------------------------------------------------------------------
@pytest.mark.parametrize("button, index", [("first", 0), ("prev10", 2), ("prev", 11),
                                           ("next", 13), ("next10", 22), ("last", 25)])
def test_a_navigation_button_sends_navigate_to_its_index(start_app, open_page, button, index):
    g = game(start_app, open_page)
    g.act({"type": "navigate", "index": 12})
    expect(g.page.locator("#move-counter")).to_have_text("12 / 25")
    since = g.mark()
    g.page.locator(f"#{button}").click()
    assert g.wait_sent("navigate", since) == {"type": "navigate", "index": index}


@pytest.mark.parametrize("button", ["first", "prev10", "prev", "undo"])
def test_backward_buttons_and_undo_are_disabled_at_cursor_0(start_app, open_page, button):
    g = game(start_app, open_page)
    g.act({"type": "navigate", "index": 0})
    expect(g.page.locator(f"#{button}")).to_be_disabled()


@pytest.mark.parametrize("button", ["next", "next10", "last"])
def test_forward_buttons_are_enabled_at_cursor_0(start_app, open_page, button):
    g = game(start_app, open_page)
    g.act({"type": "navigate", "index": 0})
    expect(g.page.locator(f"#{button}")).to_be_enabled()


@pytest.mark.parametrize("button", ["next", "next10", "last"])
def test_forward_buttons_are_disabled_at_the_end(start_app, open_page, button):
    g = game(start_app, open_page)
    expect(g.page.locator(f"#{button}")).to_be_disabled()


@pytest.mark.parametrize("button", ["first", "prev10", "prev", "undo"])
def test_backward_buttons_and_undo_are_enabled_at_the_end(start_app, open_page, button):
    g = game(start_app, open_page)
    expect(g.page.locator(f"#{button}")).to_be_enabled()


@pytest.mark.parametrize("button", ["pass", "undo", "resign"])
def test_pass_undo_and_resign_send_their_frame(start_app, open_page, button):
    g = game(start_app, open_page)
    since = g.mark()
    g.page.locator(f"#{button}").click()
    assert g.wait_sent(button, since)["type"] == button


def test_a_click_on_the_board_plays_for_the_side_to_move(start_app, open_page):
    """The canvas centre is the tengen: K10 on the fresh 19x19 board."""
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.mouse.click(*g.center())
    assert g.wait_sent("play", since) == {"type": "play", "color": "black", "vertex": "K10"}


def test_a_click_on_the_board_plays_white_when_white_is_to_move(start_app, open_page):
    from session_helpers import sgf_of

    app = start_app()
    post_sgf(app.url, sgf_of(19, "D4"))
    g = open_page(app).open()
    since = g.mark()
    g.page.mouse.click(*g.center())
    assert g.wait_sent("play", since) == {"type": "play", "color": "white", "vertex": "K10"}


# -- the move list (B13) ---------------------------------------------------------------------------------
def test_the_move_list_has_one_item_per_move(start_app, open_page):
    g = game(start_app, open_page)
    open_section(g, "moves.section")
    expect(g.page.locator("#move-list li")).to_have_count(25)


def test_a_move_item_shows_the_colour_mark_and_the_vertex(start_app, open_page):
    g = game(start_app, open_page)
    open_section(g, "moves.section")
    first = g.state()["game"]["moves"][0]
    expect(g.page.locator("#move-list li").first).to_have_text(
        re.compile(rf"^\s*●\s*{first['vertex']}\s*$"))


def test_a_white_move_item_shows_the_white_mark(start_app, open_page):
    g = game(start_app, open_page)
    open_section(g, "moves.section")
    second = g.state()["game"]["moves"][1]
    expect(g.page.locator("#move-list li").nth(1)).to_have_text(
        re.compile(rf"^\s*○\s*{second['vertex']}\s*$"))


def test_the_move_at_the_cursor_is_marked(start_app, open_page):
    g = game(start_app, open_page)
    open_section(g, "moves.section")
    g.act({"type": "navigate", "index": 7})
    expect(g.page.locator("#move-list li.current")).to_have_count(1)
    expect(g.page.locator("#move-list li").nth(6)).to_have_class(re.compile(r"\bcurrent\b"))


def test_clicking_a_move_item_navigates_to_it(start_app, open_page):
    g = game(start_app, open_page)
    open_section(g, "moves.section")
    since = g.mark()
    g.page.locator("#move-list li").nth(2).click()
    assert g.wait_sent("navigate", since) == {"type": "navigate", "index": 3}


# -- new game (B10) ----------------------------------------------------------------------------------------
def test_start_sends_the_defaults_with_a_null_komi(start_app, open_page):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    since = g.mark()
    g.page.locator("#new-game").click()
    assert g.wait_sent("new_game", since) == {"type": "new_game", "size": 19, "komi": None,
                                              "rules": "japanese", "handicap": 0}


def test_start_sends_the_chosen_size_rules_and_handicap(start_app, open_page):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    g.page.locator("#new-size").select_option("9")
    g.page.locator("#new-rules").select_option("chinese")
    g.page.locator("#new-handicap").fill("3")
    g.page.locator("#new-handicap").dispatch_event("change")
    since = g.mark()
    g.page.locator("#new-game").click()
    assert g.wait_sent("new_game", since) == {"type": "new_game", "size": 9, "komi": None,
                                              "rules": "chinese", "handicap": 3}


def test_a_typed_komi_is_sent_as_a_number(start_app, open_page):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    g.page.locator("#new-komi").fill("3.5")
    since = g.mark()
    g.page.locator("#new-game").click()
    assert g.wait_sent("new_game", since)["komi"] == 3.5


@pytest.mark.parametrize("rules, komi", [("japanese", "6.5"), ("chinese", "7.5"),
                                         ("new-zealand", "7")])
def test_the_komi_field_shows_the_rule_sets_default(start_app, open_page, rules, komi):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    g.page.locator("#new-rules").select_option(rules)
    expect(g.page.locator("#new-komi")).to_have_value(komi)


def test_the_komi_field_shows_the_handicap_komi_from_two_stones(start_app, open_page):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    g.page.locator("#new-handicap").fill("2")
    g.page.locator("#new-handicap").dispatch_event("change")
    expect(g.page.locator("#new-komi")).to_have_value("0.5")


def test_the_komi_field_keeps_a_typed_komi_when_the_size_changes(start_app, open_page):
    g = open_page(start_app()).open()
    open_section(g, "newGame.section")
    g.page.locator("#new-komi").fill("3.5")
    g.page.locator("#new-size").select_option("13")
    expect(g.page.locator("#new-komi")).to_have_value("3.5")


def test_the_komi_field_follows_the_health_rule_defaults(start_app, open_page):
    """The defaults come from /api/health ``ruleDefaults`` (§5), not from the page."""
    g = open_page(start_app())
    g.page.route("**/api/health", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**route.fetch().json(), "ruleDefaults": {"japanese": 4.5,
                                                                  "chinese": 8.5}})))
    g.open()
    open_section(g, "newGame.section")
    g.page.locator("#new-rules").select_option("chinese")
    expect(g.page.locator("#new-komi")).to_have_value("8.5")


# -- players and the engine move (B1, B2, B16) ------------------------------------------------------------
@pytest.mark.parametrize("box, field", [("black-engine", "blackIsEngine"),
                                        ("white-engine", "whiteIsEngine")])
def test_a_player_box_sends_players(start_app, open_page, box, field):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator(f"#{box}").check()
    assert g.wait_sent("players", since)[field] is True


def test_the_player_boxes_follow_the_server(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "players", "whiteIsEngine": True})
    expect(g.page.locator("#white-engine")).to_be_checked()


def test_the_engine_move_button_is_disabled_without_an_engine(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#genmove")).to_be_disabled()


def test_the_final_score_button_is_disabled_without_an_engine(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#final-score")).to_be_disabled()


def connected_gtp(start_engine, start_app, open_page):
    app = start_app(start_engine("gtp"), True)
    g = open_page(app).open()
    expect(g.page.locator("#genmove")).to_be_enabled(timeout=ENGINE)
    return g


def test_engine_move_now_sends_genmove(start_engine, start_app, open_page):
    g = connected_gtp(start_engine, start_app, open_page)
    since = g.mark()
    g.page.locator("#genmove").click()
    assert g.wait_sent("genmove", since)["type"] == "genmove"


def test_the_engine_move_arrives_on_the_board(start_engine, start_app, open_page):
    g = connected_gtp(start_engine, start_app, open_page)
    g.page.locator("#genmove").click()
    expect(g.page.locator("#move-counter")).to_have_text("1 / 1", timeout=ENGINE)


def test_the_engine_answers_a_human_move_when_it_plays_white(start_engine, start_app, open_page):
    g = connected_gtp(start_engine, start_app, open_page)
    g.page.locator("#white-engine").check()
    expect(g.page.locator("#white-engine")).to_be_checked()
    g.page.mouse.click(*g.center())
    expect(g.page.locator("#move-counter")).to_have_text("2 / 2", timeout=ENGINE)


def test_final_score_sends_final_score(start_engine, start_app, open_page):
    g = connected_gtp(start_engine, start_app, open_page)
    expect(g.page.locator("#final-score")).to_be_enabled()
    since = g.mark()
    g.page.locator("#final-score").click()
    assert g.wait_sent("final_score", since)["type"] == "final_score"


def test_the_final_score_is_shown_in_the_status(start_engine, start_app, open_page):
    g = connected_gtp(start_engine, start_app, open_page)
    g.page.locator("#final-score").click()
    expect(g.page.locator("#status")).to_contain_text("The engine scores the game",
                                                      timeout=ENGINE)


# -- captures and the side to move ----------------------------------------------------------------------
def test_both_capture_counts_start_at_zero(start_app, open_page):
    g = open_page(start_app()).open()
    assert [g.page.locator("#captures-black").inner_text(),
            g.page.locator("#captures-white").inner_text()] == ["0", "0"]


# -- the status line (§3.8 "Status line") ----------------------------------------------------------------
PAUSED = datetime(2030, 1, 1, 12, 0, 0)


def paused(start_app, open_page):
    """A page whose timers only run when the test advances the clock."""
    g = open_page(start_app())
    g.page.clock.install(time=PAUSED)
    g.page.clock.pause_at(PAUSED + timedelta(seconds=1))
    return g.open()


def test_an_error_frame_is_shown_in_the_status(start_app, open_page):
    g = open_page(start_app()).open()
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_contain_text("unknown message type")


def test_an_error_frame_is_shown_in_the_error_colour(start_app, open_page):
    g = open_page(start_app()).open()
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_have_class(re.compile(r"\berror\b"))


def test_a_state_status_is_shown_in_the_muted_colour(start_app, open_page):
    from session_helpers import sgf_of

    app = start_app()
    g = open_page(app).open()
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_have_class(re.compile(r"\berror\b"))
    g.act({"type": "load_sgf", "sgf": sgf_of(9, "E5")})
    expect(g.page.locator("#status")).not_to_have_class(re.compile(r"\berror\b"))


def test_the_status_clears_after_eight_seconds(start_app, open_page):
    g = paused(start_app, open_page)
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_contain_text("unknown message type")
    g.page.clock.run_for(7_900)
    expect(g.page.locator("#status")).to_contain_text("unknown message type")
    g.page.clock.run_for(200)
    expect(g.page.locator("#status")).to_have_text("")


def test_a_new_message_restarts_the_status_timer(start_app, open_page):
    g = paused(start_app, open_page)
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_contain_text("unknown message type")
    g.page.clock.run_for(5_000)
    g.send_raw({"type": "another-unknown"})
    expect(g.page.locator("#status")).to_contain_text("another-unknown")
    g.page.clock.run_for(5_000)
    expect(g.page.locator("#status")).to_contain_text("another-unknown")


def test_server_text_in_the_status_is_shown_as_text(start_app, open_page):
    g = open_page(start_app()).open()
    g.send_raw({"type": "<b>bold</b>"})
    expect(g.page.locator("#status")).to_contain_text("<b>bold</b>")
    expect(g.page.locator("#status b")).to_have_count(0)


# -- SGF load (B11) -----------------------------------------------------------------------------------------
def sgf_upload(text: str | bytes, name: str = "game.sgf") -> dict:
    data = text.encode("utf-8") if isinstance(text, str) else text
    return {"name": name, "mimeType": "application/x-go-sgf", "buffer": data}


def test_loading_a_file_posts_it_and_the_new_state_arrives(start_app, open_page):
    from session_helpers import random_sgf

    g = open_page(start_app()).open()
    g.page.locator("#sgf-file").set_input_files(sgf_upload(random_sgf(10, 9)))
    expect(g.page.locator("#move-counter")).to_have_text("10 / 10")


def test_loading_a_file_uses_post_api_sgf(start_app, open_page):
    from session_helpers import random_sgf

    g = open_page(start_app()).open()
    with g.page.expect_request(lambda r: r.url.endswith("/api/sgf") and r.method == "POST") \
            as request:
        g.page.locator("#sgf-file").set_input_files(sgf_upload(random_sgf(4, 9)))
    assert request.value.post_data.startswith("(;")


def test_loading_a_file_never_sends_load_sgf(start_app, open_page):
    from session_helpers import random_sgf

    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator("#sgf-file").set_input_files(sgf_upload(random_sgf(4, 9)))
    expect(g.page.locator("#move-counter")).to_have_text("4 / 4")
    g.fence()
    assert g.sent(since, "load_sgf") == []


def watch_posts(g) -> list:
    posts: list = []
    g.page.on("request", lambda r: posts.append(r.url) if r.method == "POST" else None)
    return posts


def test_a_file_over_one_mib_is_refused_in_the_page(start_app, open_page):
    g = open_page(start_app()).open()
    posts = watch_posts(g)
    g.page.locator("#sgf-file").set_input_files(sgf_upload(b"(;" + b" " * MIB + b")"))
    expect(g.page.locator("#status")).to_have_text(g.t("sgf.tooLarge"))
    g.fence()
    assert posts == []


def test_a_refused_file_is_reported_in_the_error_colour(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#sgf-file").set_input_files(sgf_upload(b"(;" + b" " * MIB + b")"))
    expect(g.page.locator("#status")).to_have_class(re.compile(r"\berror\b"))


def test_a_file_of_exactly_one_mib_is_posted(start_app, open_page):
    """1,048,576 bytes is not over the limit (§3.8): the page posts it; the server decides."""
    g = open_page(start_app()).open()
    body = b"(;GM[1]SZ[9]C[" + b"x" * (MIB - 18) + b"])"
    assert len(body) == MIB
    with g.page.expect_request(lambda r: r.url.endswith("/api/sgf") and r.method == "POST"):
        g.page.locator("#sgf-file").set_input_files(sgf_upload(body))


def test_an_sgf_the_server_cannot_read_shows_its_error_in_red(start_app, open_page):
    g = open_page(start_app()).open()
    with g.page.expect_response(lambda r: r.url.endswith("/api/sgf")) as answer:
        g.page.locator("#sgf-file").set_input_files(sgf_upload("this is not sgf"))
    message = answer.value.json()["error"]
    expect(g.page.locator("#status")).to_have_text(message)


def test_an_sgf_error_is_shown_in_the_error_colour(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#sgf-file").set_input_files(sgf_upload("this is not sgf"))
    expect(g.page.locator("#status")).to_have_class(re.compile(r"\berror\b"))


def test_an_sgf_error_message_is_shown_as_text(start_app, open_page):
    g = open_page(start_app())
    g.page.route("**/api/sgf", lambda route: route.fulfill(
        status=400, content_type="application/json", body=json.dumps({"error": "<i>no</i>"}))
        if route.request.method == "POST" else route.continue_())
    g.open()
    g.page.locator("#sgf-file").set_input_files(sgf_upload("(;)"))
    expect(g.page.locator("#status")).to_have_text("<i>no</i>")


def test_any_other_load_failure_names_the_http_status(start_app, open_page):
    g = open_page(start_app())
    g.page.route("**/api/sgf", lambda route: route.fulfill(status=502, body="bad gateway")
                 if route.request.method == "POST" else route.continue_())
    g.open()
    g.page.locator("#sgf-file").set_input_files(sgf_upload("(;)"))
    expect(g.page.locator("#status")).to_have_text(g.t("sgf.loadFailed", {"status": 502}))


# -- SGF save with a chosen name (B12, §1.5) ----------------------------------------------------------------
NO_PICKER = "delete window.showSaveFilePicker;"
DEFAULT_NAME = re.compile(r"^gowui-Board_1-\d{8}-\d{4}\.sgf$")


def test_save_suggests_the_board_and_time_name_in_the_prompt(start_app, open_page):
    g = game(start_app, open_page, 3, init=NO_PICKER)
    open_section(g, "newGame.section")
    with g.page.expect_event("dialog") as shown:
        g.page.locator("#save-sgf").click()
    suggested = shown.value.default_value
    shown.value.dismiss()
    assert DEFAULT_NAME.match(suggested), suggested


def test_save_downloads_under_the_chosen_name_with_sgf_appended(start_app, open_page):
    g = game(start_app, open_page, 3, init=NO_PICKER)
    open_section(g, "newGame.section")
    g.page.once("dialog", lambda dialog: dialog.accept("my game"))
    with g.page.expect_download() as download:
        g.page.locator("#save-sgf").click()
    assert download.value.suggested_filename == "my game.sgf"


def test_save_keeps_a_chosen_name_ending_in_sgf_in_any_case(start_app, open_page):
    g = game(start_app, open_page, 3, init=NO_PICKER)
    open_section(g, "newGame.section")
    g.page.once("dialog", lambda dialog: dialog.accept("Kifu.SGF"))
    with g.page.expect_download() as download:
        g.page.locator("#save-sgf").click()
    assert download.value.suggested_filename == "Kifu.SGF"


def test_save_writes_the_active_boards_sgf(start_app, open_page):
    g = game(start_app, open_page, 3, init=NO_PICKER)
    open_section(g, "newGame.section")
    g.page.once("dialog", lambda dialog: dialog.accept("x"))
    with g.page.expect_download() as download:
        g.page.locator("#save-sgf").click()
    text = open(download.value.path(), encoding="utf-8").read()
    assert text.startswith("(;") and text.count(";B[") + text.count(";W[") == 3


def test_save_reports_the_saved_name(start_app, open_page):
    g = game(start_app, open_page, 3, init=NO_PICKER)
    open_section(g, "newGame.section")
    g.page.once("dialog", lambda dialog: dialog.accept("mine"))
    with g.page.expect_download():
        g.page.locator("#save-sgf").click()
    expect(g.page.locator("#status")).to_have_text(g.t("sgf.saved", {"name": "mine.sgf"}))


PICKER = """
window.__picked = [];
window.showSaveFilePicker = async (options) => {
  window.__picked.push(options.suggestedName);
  if (window.__abortPicker) throw new DOMException('closed', 'AbortError');
  return { name: 'picked.sgf', createWritable: async () => ({
    write: async (text) => { window.__written = String(text); }, close: async () => {} }) };
};
"""


def test_save_opens_the_system_dialog_with_the_suggested_name(start_app, open_page):
    g = game(start_app, open_page, 3, init=PICKER)
    open_section(g, "newGame.section")
    g.page.locator("#save-sgf").click()
    g.until("() => window.__picked.length === 1")
    assert DEFAULT_NAME.match(g.page.evaluate("() => window.__picked[0]"))


def test_save_through_the_system_dialog_writes_the_sgf_and_reports_it(start_app, open_page):
    g = game(start_app, open_page, 3, init=PICKER)
    open_section(g, "newGame.section")
    g.page.locator("#save-sgf").click()
    expect(g.page.locator("#status")).to_have_text(g.t("sgf.saved", {"name": "picked.sgf"}))


def test_closing_the_system_dialog_saves_nothing_and_shows_no_error(start_app, open_page):
    g = game(start_app, open_page, 3, init=PICKER + "window.__abortPicker = true;")
    open_section(g, "newGame.section")
    g.page.locator("#save-sgf").click()
    g.until("() => window.__picked.length === 1")
    g.fence()
    status = g.page.locator("#status")
    assert (g.page.evaluate("() => window.__written === undefined"),
            "picked.sgf" in status.inner_text(),
            "error" in (status.get_attribute("class") or "").split()) == (True, False, False)


def test_a_failed_save_shows_a_red_status(start_app, open_page):
    g = open_page(start_app(), init=NO_PICKER)
    g.page.route("**/api/sgf", lambda route: route.fulfill(status=500, body="boom")
                 if route.request.method == "GET" else route.continue_())
    g.open()
    open_section(g, "newGame.section")
    g.page.locator("#save-sgf").click()
    expect(g.page.locator("#status")).to_have_class(re.compile(r"\berror\b"))


def test_the_board_name_in_the_suggested_name_has_unsafe_characters_replaced(start_app,
                                                                            open_page):
    g = game(start_app, open_page, 3, init=PICKER)
    g.act({"type": "board_rename", "id": g.state()["activeBoard"], "name": 'a/b: c*d?'})
    open_section(g, "newGame.section")
    g.page.locator("#save-sgf").click()
    g.until("() => window.__picked.length === 1")
    assert re.match(r"^gowui-a_b_c_d_-\d{8}-\d{4}\.sgf$",
                    g.page.evaluate("() => window.__picked[0]"))
