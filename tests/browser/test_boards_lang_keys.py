"""The board strip, the language and the keyboard (SPEC §3.8 "Board strip", "Language"; §3.7;
§8.5; baseline features B25 boards, B27 Korean / English, B28 shortcuts, B29 several tabs).

A tile is ``#board-list .thumb[data-id]``; its name is ``.thumb-name``, its buttons are the ✎ and
× buttons, and the rename field is the ``input`` that replaces the name.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import analysis_frame, analysis_payload, expect, move_info, post_sgf

pytestmark = pytest.mark.browser


def tiles(g):
    return g.page.locator("#board-list .thumb")


def tile(g, index: int):
    return tiles(g).nth(index)


def close_button(g, index: int):
    return tile(g, index).locator("button", has_text="×")


def edit_button(g, index: int):
    return tile(g, index).locator("button", has_text="✎")


def rename_field(g, index: int = 0):
    return tile(g, index).locator("input")


def two_boards(start_app, open_page, **options):
    g = open_page(start_app(), **options).open()
    g.page.locator("#board-duplicate").click()
    expect(tiles(g)).to_have_count(2)
    return g


def board_ids(g) -> list[int]:
    return [b["id"] for b in g.state()["boards"]]


# -- tiles (B25) -----------------------------------------------------------------------------------------
def test_duplicate_sends_board_duplicate(start_app, open_page):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator("#board-duplicate").click()
    assert g.wait_sent("board_duplicate", since) == {"type": "board_duplicate"}


def test_each_tile_carries_its_boards_id(start_app, open_page):
    g = two_boards(start_app, open_page)
    ids = [tile(g, i).get_attribute("data-id") for i in range(2)]
    assert ids == [str(i) for i in board_ids(g)]


def test_clicking_a_tile_selects_its_board(start_app, open_page):
    g = two_boards(start_app, open_page)
    since = g.mark()
    tile(g, 0).click()
    assert g.wait_sent("board_select", since) == {"type": "board_select", "id": board_ids(g)[0]}


def test_the_only_board_has_no_close_button(start_app, open_page):
    g = open_page(start_app()).open()
    expect(tiles(g)).to_have_count(1)
    expect(close_button(g, 0)).to_be_hidden()


def test_two_boards_have_close_buttons(start_app, open_page):
    g = two_boards(start_app, open_page)
    expect(close_button(g, 0)).to_be_visible()


def test_close_asks_then_sends_board_delete(start_app, open_page):
    g = two_boards(start_app, open_page)
    first = board_ids(g)[0]
    dialogs = []
    g.page.once("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
    since = g.mark()
    close_button(g, 0).click()
    assert (g.wait_sent("board_delete", since), dialogs) == \
        ({"type": "board_delete", "id": first}, ["confirm"])


def test_declining_the_close_sends_nothing(start_app, open_page):
    g = two_boards(start_app, open_page)
    g.page.once("dialog", lambda dialog: dialog.dismiss())
    since = g.mark()
    close_button(g, 0).click()
    g.fence()
    assert g.sent(since) == []


def test_a_deleted_board_loses_its_tile(start_app, open_page):
    g = two_boards(start_app, open_page)
    g.act({"type": "board_delete", "id": board_ids(g)[0]})
    expect(tiles(g)).to_have_count(1)


def test_a_tile_shows_the_move_and_total(start_app, open_page):
    from session_helpers import sgf_of

    app = start_app()
    post_sgf(app.url, sgf_of(9, "E5", "C3", "G7"))
    g = open_page(app).open()
    g.act({"type": "navigate", "index": 2})
    expect(tile(g, 0)).to_contain_text(g.t("boards.move", {"n": 2, "total": 3}))


def test_a_tile_shows_the_profile_and_identity_tuple(start_app, open_page):
    g = open_page(start_app()).open()
    expect(tile(g, 0)).to_contain_text(f"preaz_1d · {g.t('boards.identity')}")


def test_a_tile_shows_its_tuple_as_key_value_pairs(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "human_params", "policy": {"min_p": 0.05}})
    expect(tile(g, 0)).to_contain_text(re.compile(r"preaz_1d · min\S*\s+0\.05"))


def test_a_tile_says_a_b_while_its_board_compares(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "human_params", "policy": {}, "compare": {"temperature": 1.5}})
    expect(tile(g, 0)).to_contain_text("A/B")


def test_a_tile_is_updated_in_place(start_app, open_page):
    g = two_boards(start_app, open_page)
    tile(g, 0).evaluate("(el) => { el.__probe = 'kept'; }")
    g.act({"type": "board_rename", "id": board_ids(g)[0], "name": "Renamed"})
    expect(tile(g, 0)).to_contain_text("Renamed")
    assert tile(g, 0).evaluate("(el) => el.__probe") == "kept"


def test_an_unchanged_tile_is_not_redrawn(start_app, open_page):
    g = two_boards(start_app, open_page)
    canvas = "#board-list .thumb:nth-child(1) canvas"
    before = g.draws(canvas)
    for _ in range(3):
        g.fence()
    assert g.draws(canvas) == before


def test_a_tile_is_redrawn_when_its_stones_change(start_app, open_page):
    g = open_page(start_app()).open()
    canvas = "#board-list .thumb:nth-child(1) canvas"
    before = g.draws(canvas)
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    g.until("([sel, n]) => +document.querySelector(sel).dataset.draws > n",
            arg=[canvas, before])


# -- rename in place (B25) ---------------------------------------------------------------------------------
def start_rename(g, index: int = 0, *, pencil: bool = False) -> None:
    if pencil:
        edit_button(g, index).click()
    else:
        tile(g, index).locator(".thumb-name").dblclick()
    expect(rename_field(g, index)).to_be_focused()


def test_double_clicking_the_name_opens_the_rename_field(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    expect(rename_field(g)).to_be_visible()


def test_the_pencil_opens_the_rename_field(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g, pencil=True)
    expect(rename_field(g)).to_be_visible()


def test_the_rename_field_takes_at_most_40_characters(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    assert rename_field(g).get_attribute("maxlength") == "40"


def test_enter_saves_the_new_name(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    since = g.mark()
    rename_field(g).fill("Joseki")
    rename_field(g).press("Enter")
    assert g.wait_sent("board_rename", since) == {"type": "board_rename",
                                                  "id": board_ids(g)[0], "name": "Joseki"}


def test_leaving_the_field_saves_the_new_name(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    since = g.mark()
    rename_field(g).fill("Blurred")
    g.page.locator("#move-counter").click()
    assert g.wait_sent("board_rename", since)["name"] == "Blurred"


def test_escape_cancels_the_rename(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    since = g.mark()
    rename_field(g).fill("Never")
    rename_field(g).press("Escape")
    expect(rename_field(g)).to_have_count(0)
    g.fence()
    assert g.sent(since, "board_rename") == []


def test_an_empty_name_sends_nothing(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    since = g.mark()
    rename_field(g).fill("   ")
    rename_field(g).press("Enter")
    g.fence()
    assert g.sent(since, "board_rename") == []


def test_an_unchanged_name_sends_nothing(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "board_rename", "id": board_ids(g)[0], "name": "Kept"})
    expect(tile(g, 0)).to_contain_text("Kept")
    start_rename(g)
    expect(rename_field(g)).to_have_value("Kept")
    since = g.mark()
    rename_field(g).press("Enter")
    g.fence()
    assert g.sent(since, "board_rename") == []


def test_a_default_name_opens_an_empty_field_with_the_localised_placeholder(start_app,
                                                                           open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    assert (rename_field(g).input_value(), rename_field(g).get_attribute("placeholder")) == \
        ("", g.t("boards.default", {"n": 1}))


def test_enter_at_once_on_a_default_name_saves_nothing(start_app, open_page):
    g = open_page(start_app(), locale="ko-KR").open()
    start_rename(g)
    since = g.mark()
    rename_field(g).press("Enter")
    g.fence()
    assert g.sent(since, "board_rename") == []


def test_keys_typed_in_the_rename_field_never_reach_the_shortcuts(start_app, open_page):
    g = two_boards(start_app, open_page)
    start_rename(g, 1)
    since = g.mark()
    for key in ["[", "]", "p", "u", "g", "a", "Home", "End", "ArrowLeft", "ArrowRight"]:
        rename_field(g, 1).press(key)
    g.fence()
    assert g.sent(since) == []


def test_an_open_rename_field_survives_state_frames(start_app, open_page):
    g = open_page(start_app()).open()
    start_rename(g)
    rename_field(g).fill("half typed")
    for _ in range(5):
        g.fence()
    expect(rename_field(g)).to_have_value("half typed")
    expect(rename_field(g)).to_be_focused()


def test_an_open_rename_field_survives_analysis_frames(start_app, open_page):
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.act({"type": "analysis", "enabled": True})
    start_rename(g)
    rename_field(g).fill("half typed")
    for n in range(20):
        payload = analysis_payload(19, [move_info("D4", visits=10 + n)],
                                   policy=[0.001] * (19 * 19 + 1))
        g.inject(analysis_frame(g.state(), payload))
    g.until("() => window.__lastAnalysis && "
            "window.__lastAnalysis.moveInfos[0].visits === 29")
    expect(rename_field(g)).to_have_value("half typed")
    expect(rename_field(g)).to_be_focused()


# -- default names (§3.8 "Default names") ---------------------------------------------------------------------
@pytest.mark.parametrize("locale, shown", [("en-US", "Board 1"), ("ko-KR", "보드 1")])
def test_a_default_name_is_shown_localised(start_app, open_page, locale, shown):
    g = open_page(start_app(), locale=locale).open()
    expect(tile(g, 0).locator(".thumb-name")).to_have_text(shown)


@pytest.mark.parametrize("name", ["Board 3a", "board 3", "Board  3", "My Board 3"])
def test_a_name_that_is_not_exactly_board_n_is_shown_as_it_is(start_app, open_page, name):
    g = open_page(start_app(), locale="ko-KR").open()
    g.act({"type": "board_rename", "id": board_ids(g)[0], "name": name})
    expect(tile(g, 0).locator(".thumb-name")).to_have_text(name)


def test_a_board_name_is_shown_as_text(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "board_rename", "id": board_ids(g)[0], "name": "<b>x</b>"})
    expect(tile(g, 0).locator(".thumb-name")).to_have_text("<b>x</b>")
    expect(tile(g, 0).locator("b")).to_have_count(0)


# -- several tabs (B29) -------------------------------------------------------------------------------------------
def test_a_move_in_one_tab_shows_in_the_other(start_app, open_page):
    app = start_app()
    first = open_page(app).open()
    second = open_page(app).open()
    first.page.locator("#pass").click()
    expect(second.page.locator("#move-counter")).to_have_text("1 / 1")


# -- language (B27, §8.5) ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("locale, lang", [("en-US", "en"), ("en-GB", "en"), ("ko-KR", "ko"),
                                          ("fr-FR", "ko")])
def test_the_initial_language_follows_the_browser(start_app, open_page, locale, lang):
    g = open_page(start_app(), locale=locale).open()
    expect(g.page.locator("html")).to_have_attribute("lang", lang)


def test_an_upper_case_en_browser_language_is_english(start_app, open_page):
    init = "Object.defineProperty(navigator, 'language', { get: () => 'EN-us' });"
    g = open_page(start_app(), locale="ko-KR", init=init).open()
    expect(g.page.locator("html")).to_have_attribute("lang", "en")


def test_the_saved_language_wins_over_the_browser(start_app, open_page):
    g = open_page(start_app(), locale="ko-KR", storage={"gowui.lang": "en"}).open()
    expect(g.page.locator("html")).to_have_attribute("lang", "en")


def test_a_saved_language_without_a_table_is_ignored(start_app, open_page):
    g = open_page(start_app(), locale="en-US", storage={"gowui.lang": "fr"}).open()
    expect(g.page.locator("html")).to_have_attribute("lang", "en")


def test_the_select_switches_the_page_at_once(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("#pass")).to_have_text("패스")


def test_the_select_sets_the_html_lang(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("html")).to_have_attribute("lang", "ko")


def test_the_select_saves_the_choice(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("#pass")).to_have_text("패스")
    assert g.page.evaluate("() => localStorage.getItem('gowui.lang')") == "ko"


def test_the_choice_is_remembered_after_a_reload(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("#pass")).to_have_text("패스")
    g.page.reload()
    g.ready()
    expect(g.page.locator("#pass")).to_have_text("패스")


def test_switching_language_re_renders_the_board_names(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(tile(g, 0).locator(".thumb-name")).to_have_text("보드 1")


def test_switching_language_re_renders_the_dynamic_texts(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("#engine-state")).to_have_text("연결 안 됨")


def test_server_text_is_shown_as_it_arrives(start_app, open_page):
    g = open_page(start_app(), locale="ko-KR").open()
    g.send_raw({"type": "no-such-type"})
    expect(g.page.locator("#status")).to_contain_text("unknown message type")


# -- shortcuts (B28, §3.7) ------------------------------------------------------------------------------------
def mid_game(start_app, open_page, **options):
    from session_helpers import random_sgf

    app = start_app()
    post_sgf(app.url, random_sgf(10, 9))
    g = open_page(app, **options).open()
    g.act({"type": "navigate", "index": 5})
    expect(g.page.locator("#move-counter")).to_have_text("5 / 10")
    return g


@pytest.mark.parametrize("key, frame", [
    ("ArrowLeft", {"type": "navigate", "index": 4}),
    ("ArrowRight", {"type": "navigate", "index": 6}),
    ("Home", {"type": "navigate", "index": 0}),
    ("End", {"type": "navigate", "index": 10}),
    ("p", {"type": "pass"}),
    ("u", {"type": "undo"}),
    ("g", {"type": "genmove"}),
    ("a", {"type": "analysis", "enabled": True}),
])
def test_a_shortcut_sends_its_frame(start_app, open_page, key, frame):
    g = mid_game(start_app, open_page)
    g.page.locator("#board").hover()
    since = g.mark()
    g.page.keyboard.press(key)
    sent = g.wait_sent(frame["type"], since)
    assert {k: sent[k] for k in frame} == frame


@pytest.mark.parametrize("key, index", [("[", 0), ("]", 2)])
def test_a_bracket_selects_the_neighbouring_board(start_app, open_page, key, index):
    g = two_boards(start_app, open_page)
    g.page.locator("#board-duplicate").click()
    expect(tiles(g)).to_have_count(3)
    g.act({"type": "board_select", "id": board_ids(g)[1]})
    since = g.mark()
    g.page.keyboard.press(key)
    assert g.wait_sent("board_select", since)["id"] == board_ids(g)[index]


@pytest.mark.parametrize("target", ["#max-visits", "#label-mode", "#new-komi"])
def test_a_shortcut_is_ignored_while_a_field_has_focus(start_app, open_page, target):
    g = mid_game(start_app, open_page)
    if target == "#new-komi":
        g.page.locator("details:has(#new-komi) > summary").click()
    g.page.locator(target).focus()
    since = g.mark()
    g.page.keyboard.press("p")
    g.page.keyboard.press("u")
    g.fence()
    assert g.sent(since, "pass") + g.sent(since, "undo") == []


def test_a_shortcut_is_ignored_before_the_first_state(start_app, open_page):
    """The socket is open to the real server, but the test withholds the server's frames, so
    no ``state`` arrives; a key that were handled would put a frame on the open socket."""
    g = open_page(start_app())
    withheld = []

    def handler(route):
        server = route.connect_to_server()
        route.on_message(lambda message: server.send(message))
        server.on_message(withheld.append)

    g.page.route_web_socket(re.compile(r".*/ws$"), handler)
    g.goto()
    g.until("() => window.__gowuiTest.sockets.length > 0 && "
            "window.__gowuiTest.sockets[0].readyState === 1")
    g.page.locator("body").press("p")
    g.page.locator("body").press("Home")
    assert g.sent(0) == []


def test_a_handled_key_does_not_scroll_the_page(start_app, open_page):
    g = mid_game(start_app, open_page)
    g.page.evaluate("() => window.addEventListener('keydown', (e) => {"
                    " window.__prevented = e.defaultPrevented; })")
    g.page.locator("#board").hover()
    g.page.keyboard.press("End")
    assert g.page.evaluate("() => window.__prevented") is True


# -- analysis follows the board on screen (B26, §3.3) ----------------------------------------------
def test_analysis_follows_the_board_on_screen(start_engine, start_app, open_page):
    from browser_kit import ENGINE

    g = open_page(start_app(start_engine("gtp"), True)).open()
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    g.page.locator("#board-duplicate").click()
    expect(tile(g, 1)).to_have_class(re.compile(r"\bactive\b"))
    since = g.mark()
    g.wait_received("analysis", since, timeout=ENGINE)
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
