"""Browser tours of the baseline features (SPEC §11.1, §3.8; issue #11).

Each test starts one app (and one fake engine, §2.6) and walks a group of §11 rows through the
page's own controls, asserting each row's visible or draw-record (§3.8 "Test observability")
outcome briefly. No test sleeps: waits are Playwright expectations, the frame recorder and the
fence of browser_kit.py.

- ``test_play_tour``: B1, B2, B10, B12, B13, B14, B32, B33.
- ``test_fast_engines_stop_when_both_players_are_unticked``: B1 against an instant engine (§4.3).
- ``test_analysis_tour``: B6, B7, B8, B18, B26, B31.
- ``test_handol_tour``: B3, B19, B21, B22, B23, B24.
- ``test_log_tour``: B30.
"""

from __future__ import annotations

import json
import re
import time

import pytest

from browser_kit import ENGINE, QUICK, Gowui, expect, post_sgf

pytestmark = pytest.mark.browser

#: The side panel's label "black x% / white y%" (§3.8 "Evaluation").
WINBAR = re.compile(r"^black (\d+\.\d)%\s+/\s+white (\d+\.\d)%$")


# -- helpers ------------------------------------------------------------------------------------------
def connected(start_engine, start_app, open_page, protocol: str = "gtp", *engine: str,
              **page) -> Gowui:
    app = start_app(start_engine(protocol, *engine), True)
    g = open_page(app, **page).open()
    expect(g.page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    return g


def wait_state(g: Gowui, predicate, timeout: int = QUICK) -> dict:
    """The newest ``state`` the page received once it satisfies ``predicate``."""
    deadline = time.monotonic() + timeout / 1000
    while True:
        state = g.last_received("state")
        if state is not None and predicate(state):
            return state
        if time.monotonic() > deadline:
            raise AssertionError(f"no state satisfied the predicate; last {state}")
        g.page.wait_for_timeout(25)


def click_point(g: Gowui, x: int, y: int, size: int) -> None:
    """Click point (``x``, ``y``) counted from the top left, with board.js's geometry."""
    box = g.page.locator("#board").bounding_box()
    margin = box["width"] / (size + 1.6)
    cell = (box["width"] - 2 * margin) / (size - 1)
    scale = box["height"] / box["width"]
    g.page.mouse.click(box["x"] + margin + x * cell, box["y"] + (margin + y * cell) * scale)


def open_section(g: Gowui, inner: str) -> None:
    """Open the collapsible side-panel section holding ``inner``."""
    details = g.page.locator(f"details:has({inner})").last
    if details.get_attribute("open") is None:
        details.locator("> summary").click()


def untick_engines(g: Gowui) -> None:
    """Untick both "KataGo plays" boxes and wait for the server's settings to agree."""
    for box, key in (("#black-engine", "blackIsEngine"), ("#white-engine", "whiteIsEngine")):
        since = g.mark()
        g.page.locator(box).click()
        g.wait_sent("players", since, {key: False})
    wait_state(g, lambda s: not s["settings"]["blackIsEngine"]
               and not s["settings"]["whiteIsEngine"], timeout=ENGINE)


def counter(g: Gowui, text: str, timeout: int = QUICK) -> None:
    expect(g.page.locator("#move-counter")).to_have_text(text, timeout=timeout)


# -- play ----------------------------------------------------------------------------------------------
def test_play_tour(start_engine, start_app, open_page):
    # An instant engine, unpaced: state and analysis frames are coalesced and log frames fold
    # into one log_history (§4.3), so engine-vs-engine never closes the tab with 1013.
    g = connected(start_engine, start_app, open_page, "gtp")
    page = g.page

    # B10: new game with size, handicap, komi and rules; the server applies them.
    open_section(g, "#new-game")
    page.locator("#new-size").select_option("13")
    page.locator("#new-handicap").fill("2")
    page.locator("#new-handicap").dispatch_event("change")
    since = g.mark()
    page.locator("#new-game").click()
    assert g.wait_sent("new_game", since) == {"type": "new_game", "size": 13, "komi": None,
                                                "rules": "japanese", "handicap": 2}
    game = wait_state(g, lambda s: s["game"]["size"] == 13)["game"]
    assert game["handicap"] == 2 and len(game["setupStones"]) == 2 and game["toPlay"] == "white"

    page.locator("#new-size").select_option("9")
    page.locator("#new-handicap").fill("0")
    page.locator("#new-handicap").dispatch_event("change")
    page.locator("#new-rules").select_option("chinese")
    page.locator("#new-komi").fill("5.5")
    page.locator("#new-komi").dispatch_event("input")
    since = g.mark()
    page.locator("#new-game").click()
    assert g.wait_sent("new_game", since) == {"type": "new_game", "size": 9, "komi": 5.5,
                                                "rules": "chinese", "handicap": 0}
    game = wait_state(g, lambda s: s["game"]["size"] == 9)["game"]
    assert (game["komi"], game["rules"], game["handicap"]) == (5.5, "chinese", 0)
    counter(g, "0 / 0")

    # B1 (human vs human) and B33: clicks alternate colours; black captures the corner stone.
    for n, (x, y) in enumerate([(2, 2), (6, 6), (0, 1), (0, 0), (1, 0)], start=1):
        click_point(g, x, y, 9)
        counter(g, f"{n} / {n}")
    game = g.state()["game"]
    assert game["stones"][0] == 0, "the white corner stone is captured"
    assert game["captures"] == {"black": 1, "white": 0}
    expect(page.locator("#captures-black")).to_have_text("1")
    expect(page.locator("#captures-white")).to_have_text("0")

    # B32: move numbers on the stones, toggleable.
    page.locator("#show-numbers").check()
    g.expect_dataset("numbers", "on")
    page.locator("#show-numbers").uncheck()
    g.expect_dataset("numbers", "off")

    # B13: every navigation button, then a click in the move list.
    for button, text in [("#first", "0 / 5"), ("#next", "1 / 5"), ("#next10", "5 / 5"),
                         ("#prev10", "0 / 5"), ("#last", "5 / 5"), ("#prev", "4 / 5")]:
        page.locator(button).click()
        counter(g, text)
    open_section(g, "#move-list")
    items = page.locator("#move-list li")
    expect(items).to_have_count(5)
    expect(items.nth(0)).to_have_text("● C7")
    items.nth(1).click()
    counter(g, "2 / 5")
    expect(items.nth(1)).to_have_class(re.compile(r"\bcurrent\b"))
    page.locator("#last").click()
    counter(g, "5 / 5")

    # B14: undo takes the capture back; pass adds a pass move.
    page.locator("#undo").click()
    counter(g, "4 / 4")
    expect(page.locator("#captures-black")).to_have_text("0")
    page.locator("#pass").click()
    counter(g, "5 / 5")
    assert g.state()["game"]["moves"][-1]["vertex"].lower() == "pass"

    # B2: "Engine move now" plays for the side to move (white, after Black's pass).
    page.locator("#genmove").click()
    counter(g, "6 / 6", timeout=ENGINE)
    assert g.state()["game"]["moves"][-1]["color"] == "white"

    # B1 (human vs engine): with KataGo on White, a human black move gets an engine answer.
    since = g.mark()
    page.locator("#white-engine").check()
    assert g.wait_sent("players", since) == {"type": "players", "whiteIsEngine": True}
    wait_state(g, lambda s: s["settings"]["whiteIsEngine"])
    empty = [i for i, v in enumerate(g.state()["game"]["stones"]) if v == 0]
    click_point(g, empty[-1] % 9, empty[-1] // 9, 9)
    counter(g, "8 / 8", timeout=ENGINE)
    assert [m["color"] for m in g.state()["game"]["moves"][-2:]] == ["black", "white"]

    # B1 (engine vs engine): with both colours on the engine, moves come without a click.
    page.locator("#black-engine").check()
    wait_state(g, lambda s: s["game"]["moveCount"] >= 10 or s["game"]["gameOver"],
               timeout=ENGINE)
    untick_engines(g)
    assert g.closes(0) == [], "the tab kept up: no overflow close (§4.3)"

    # B12: save with a user-chosen name (the name-prompt path of §3.8 "SGF").
    page.evaluate("() => { window.showSaveFilePicker = undefined; }")
    page.once("dialog", lambda dialog: dialog.accept("my-game"))
    with page.expect_download() as download:
        page.locator("#save-sgf").click()
    assert download.value.suggested_filename == "my-game.sgf"
    text = open(download.value.path(), encoding="utf-8").read()
    assert "SZ[9]" in text and "KM[5.5]" in text
    expect(page.locator("#status")).to_have_text("Saved my-game.sgf")

    # B14: resign ends the game.
    if not g.state()["game"]["gameOver"]:
        page.locator("#resign").click()
        game = wait_state(g, lambda s: s["game"]["gameOver"])["game"]
        assert game["result"].endswith("+R")


def test_fast_engines_stop_when_both_players_are_unticked(start_engine, start_app, open_page):
    """B1 against an instant engine through a long run (§4.3 "Coalescing", "Log folding"): the
    flood of ``state`` frames is coalesced and the ``log`` frames fold, so the tab keeps its
    socket and its Untick clicks stop the engines."""
    g = connected(start_engine, start_app, open_page)
    page = g.page
    page.locator("#white-engine").check()
    page.locator("#black-engine").check()
    wait_state(g, lambda s: s["game"]["moveCount"] >= 150 or s["game"]["gameOver"],
               timeout=ENGINE)
    assert g.closes(0) == [], "no overflow close during the long run (§4.3)"
    untick_engines(g)
    g.fence()
    stopped = g.state()["game"]["moveCount"]
    g.fence()
    assert (g.state()["game"]["moveCount"], g.closes(0)) == (stopped, [])
    assert stopped < 2000, "the engines stopped well before the move cap"


# -- analysis ------------------------------------------------------------------------------------------
def test_analysis_tour(start_engine, start_app, open_page):
    # The analysis-engine protocol: its answers carry the raw policy the heatmap needs (§2.4).
    g = connected(start_engine, start_app, open_page, "analysis")
    page = g.page

    # B31: the winrate bar, score lead and visit count of the position.
    expect(page.locator("#winbar-label")).to_have_text("--")
    page.locator("#analysis-on").check()
    expect(page.locator("#winbar-label")).to_have_text(WINBAR, timeout=ENGINE)
    expect(page.locator("#score-lead")).to_have_text(re.compile(r"^[BW]\+\d+\.\d$"))
    expect(page.locator("#visit-count")).to_have_text(re.compile(r"^\d+(\.\d)?[kM]? visits$"))
    width = page.evaluate("() => document.getElementById('winbar-black').style.width")
    assert re.fullmatch(r"\d+(\.\d)?%", width)

    # B8: after a black move (White to move) the bar still shows Black's winrate: the label's
    # black part is the root winrate the page holds, which the server keeps from Black's view.
    g.act({"type": "play", "color": "black", "vertex": "D4"})
    g.until("() => window.__lastAnalysis && window.__lastAnalysis.currentPlayer !== undefined"
            " && document.getElementById('winbar-label').textContent.startsWith('black')",
            timeout=ENGINE)
    g.until("() => { const a = window.__lastAnalysis; const label = document.getElementById("
            "'winbar-label').textContent; return a && a.rootInfo && label.startsWith('black ' + "
            "(a.rootInfo.winrate * 100).toFixed(1) + '%'); }", timeout=ENGINE)

    # B6: the raw policy heatmap.
    page.locator("#show-policy").check()
    g.expect_dataset("heatmap", "policy", timeout=ENGINE)
    page.locator("#show-policy").uncheck()
    g.expect_dataset("heatmap", "off")

    # B18 and B7: max visits, report interval and ownership are the server's settings.
    since = g.mark()
    page.locator("#max-visits").fill("123")
    page.locator("#max-visits").press("Enter")
    assert g.wait_sent("engine_params", since)["maxVisits"] == 123
    wait_state(g, lambda s: s["settings"]["maxVisits"] == 123)
    since = g.mark()
    page.locator("#interval").fill("0.8")
    page.locator("#interval").press("Enter")
    assert g.wait_sent("engine_params", since)["reportInterval"] == 0.8
    wait_state(g, lambda s: s["settings"]["reportInterval"] == 0.8)
    since = g.mark()
    page.locator("#show-ownership").check()
    assert g.wait_sent("engine_params", since)["includeOwnership"] is True
    wait_state(g, lambda s: s["settings"]["includeOwnership"])
    g.expect_dataset("ownership", "on", timeout=ENGINE)
    page.locator("#show-ownership").uncheck()
    g.expect_dataset("ownership", "off", timeout=ENGINE)

    # B26: analysis follows the board on screen. The duplicate gets a second move; its analysis
    # is for cursor 2, and selecting the first board brings analysis for its cursor 1 back.
    page.locator("#board-duplicate").click()
    expect(page.locator("#board-list .thumb")).to_have_count(2)
    wait_state(g, lambda s: s["activeBoard"] == s["boards"][1]["id"])
    g.act({"type": "play", "color": "white", "vertex": "Q16"})
    since = g.mark()
    assert g.wait_received("analysis", since, timeout=ENGINE)["cursor"] == 2
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    page.locator("#board-list .thumb").nth(0).click()
    first = wait_state(g, lambda s: s["activeBoard"] == s["boards"][0]["id"])
    assert first["game"]["moveCount"] == 1
    since = g.mark()
    assert g.wait_received("analysis", since, timeout=ENGINE)["cursor"] == 1
    assert all(f["cursor"] == 1 for f in g.received(since, "analysis"))
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)


# -- handol-mux ----------------------------------------------------------------------------------------
def test_handol_tour(start_engine, start_app, open_page, tmp_path):
    g = connected(start_engine, start_app, open_page, "handol")
    page = g.page
    panel = page.locator("details.human-only")
    expect(panel).to_be_visible()
    expect(page.locator("#black-style")).to_be_visible()

    # B19: the profile field suggests the known profiles, and "?" explains the typed one.
    assert page.locator("#profile-list option").count() > 0
    page.locator("#profile-help").hover()
    card = page.locator(".help-pop")
    expect(card).to_be_visible()
    expect(card).to_contain_text("preaz_1d")
    page.mouse.move(0, 0)
    expect(card).to_be_hidden()
    since = g.mark()
    page.locator("#human-profile").fill("rank_5k")
    page.locator("#human-profile").press("Enter")
    assert g.wait_sent("human_params", since, {"profile": "rank_5k"})
    wait_state(g, lambda s: s["settings"]["humanProfile"] == "rank_5k")

    # B23: the distribution's winrate comes from the eval-visits query.
    since = g.mark()
    page.locator("#eval-visits").fill("50")
    page.locator("#eval-visits").press("Enter")
    assert g.wait_sent("human_params", since, {"evalVisits": 50})
    page.locator("#analysis-on").check()
    expect(page.locator("#winbar-label")).to_have_text(WINBAR, timeout=ENGINE)
    g.expect_dataset("labelMode", "prior")        # the human panel's first showing (§3.8)
    g.expect_dataset("heatmap", "policy", timeout=ENGINE)

    # B22: two tuples compared, with the difference view and the A / B / Δ columns.
    since = g.mark()
    page.locator("#compare-on").check()
    expect(page.locator("#tuple-tabs")).to_be_visible()
    expect(page.locator("#compare-view")).to_have_value("diff")
    sent = g.wait_sent("human_params", since, timeout=ENGINE)
    assert sent["compare"] is not None
    since = g.mark()
    page.locator("#tuple-tabs [data-slot=B]").click()
    page.locator("#human-preset").select_option("builtin:flat")
    sent = g.wait_sent("human_params", since, timeout=ENGINE)
    assert (sent["policy"], sent["compare"]) == ({}, {"temperature": 1.5})
    g.expect_dataset("heatmap", "diff", timeout=ENGINE)
    expect(page.locator("table.candidates thead th")).to_have_text(
        ["Move", "Win", "Score", "A", "B", "Δ"], timeout=ENGINE)
    page.locator("#compare-view").select_option("A")
    g.expect_dataset("heatmap", "policy")
    since = g.mark()
    page.locator("#compare-on").uncheck()
    assert g.wait_sent("human_params", since)["compare"] is None
    expect(page.locator("#tuple-tabs")).to_be_hidden()

    # B21: a built-in preset, then save, export, delete and import of the user's own.
    since = g.mark()
    page.locator("#human-preset").select_option("builtin:sharp")
    assert g.wait_sent("human_params", since, timeout=ENGINE)["policy"] == {"temperature": 0.5}
    page.once("dialog", lambda dialog: dialog.accept("mine"))
    page.locator("#preset-save").click()
    expect(page.locator("#human-preset")).to_have_value("user:mine")
    stored = lambda: json.loads(page.evaluate(  # noqa: E731
        "() => localStorage.getItem('gowui.userPresets') || '[]'"))
    assert stored() == [{"name": "mine", "tuple": {"temperature": 0.5}}]
    with page.expect_download() as download:
        page.locator("#preset-export").click()
    assert download.value.suggested_filename == "gowui-presets.json"
    exported = tmp_path / "gowui-presets.json"
    download.value.save_as(exported)
    assert json.loads(exported.read_text()) == {"gowuiPresets": 1, "presets": stored()}
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#preset-delete").click()
    expect(page.locator("#human-preset option[value='user:mine']")).to_have_count(0)
    assert stored() == []
    with page.expect_file_chooser() as chooser:
        page.locator("#preset-import").click()
    chooser.value.set_files(str(exported))
    expect(page.locator("#status")).to_have_text("Imported 1 preset(s), skipped 0")
    expect(page.locator("#human-preset option[value='user:mine']")).to_have_count(1)
    assert stored() == [{"name": "mine", "tuple": {"temperature": 0.5}}]

    # B3: the move style per colour; an engine move for each colour with its style.
    page.locator("#analysis-on").uncheck()
    since = g.mark()
    page.locator("#black-style").select_option("katago")
    assert g.wait_sent("players", since) == {"type": "players", "blackStyle": "katago"}
    wait_state(g, lambda s: s["settings"]["blackStyle"] == "katago"
               and not s["settings"]["analysisEnabled"])
    expect(page.locator("#white-style")).to_have_value("human")
    page.locator("#genmove").click()
    counter(g, "1 / 1", timeout=ENGINE)
    page.locator("#genmove").click()
    counter(g, "2 / 2", timeout=ENGINE)
    assert [m["color"] for m in g.state()["game"]["moves"]] == ["black", "white"]

    # B24: a setup whose side to move the surface would guess wrong is refused with a reason.
    post_sgf(g.url, "(;GM[1]FF[4]SZ[19]AB[dd]PL[W])")
    wait_state(g, lambda s: s["game"]["moveCount"] == 0 and s["game"]["toPlay"] == "white")
    page.locator("#analysis-on").check()
    expect(page.locator("#status")).to_contain_text("play a move first", timeout=ENGINE)


# -- the traffic log -----------------------------------------------------------------------------------
def test_log_tour(start_engine, start_app, open_page):
    g = connected(start_engine, start_app, open_page, "gtp")
    page = g.page
    open_section(g, "#log")
    log = page.locator("#log > div")

    # B30: the connect handshake is in the log history; sent lines are marked ▸.
    expect(page.locator("#log > div.send").first).to_have_text(re.compile(r"^▸ "))
    before = log.count()
    since = g.mark()
    page.locator("#raw").fill("name")
    page.locator("#raw-form button[type=submit]").click()
    assert g.wait_sent("raw", since) == {"type": "raw", "command": "name"}
    expect(page.locator("#log > div.send").last).to_have_text(re.compile(r"^▸ .*name"),
                                                               timeout=ENGINE)
    expect(page.locator("#log > div.recv").last).to_be_attached()
    assert log.count() > before

    # A reload rebuilds the log from log_history (§3.6).
    count = log.count()
    page.reload()
    g.ready()
    open_section(g, "#log")
    expect(page.locator("#log > div.send").last).to_have_text(re.compile(r"^▸ .*name"))
    assert 0 < log.count() <= count + 1
