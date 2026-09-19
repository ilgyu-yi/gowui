"""Both-mode smoke run (issue #11; SPEC §3.8, §8, §6.1): in each launch mode, against the fake
engine, the page connects, plays, analyses, and — after the app restarts on the same storage
(local: the same ``--state`` file; server: the same database, signed in to the same account) —
shows the same board with the engine reconnected and the analysis back.

Each run writes a screenshot of its mode to the gitignored ``test-artifacts/``; with
``GOWUI_UPDATE_SCREENSHOTS=1`` it writes the committed ``docs/screenshots/<mode>-mode.png``
instead, so an ordinary run never dirties the tree.
"""

from __future__ import annotations

import os
import re

import pytest

from browser_kit import ARTIFACTS, ENGINE, REPO, Gowui, expect

pytestmark = pytest.mark.browser

SCREENSHOTS = (REPO / "docs" / "screenshots"
               if os.environ.get("GOWUI_UPDATE_SCREENSHOTS") == "1" else ARTIFACTS)
CANDIDATES = re.compile(r"^[1-9]\d*$")


def play_and_analyse(g: Gowui) -> dict:
    """Connected already: two clicks on the board, then analysis on. Returns the game."""
    page = g.page
    expect(page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    x, y = g.center()
    page.mouse.click(x, y)                       # black at tengen
    expect(page.locator("#move-counter")).to_have_text("1 / 1")
    box = page.locator("#board").bounding_box()
    page.mouse.click(x + box["width"] / 4, y - box["height"] / 4)
    expect(page.locator("#move-counter")).to_have_text("2 / 2")
    page.locator("#analysis-on").check()
    g.expect_dataset("candidates", CANDIDATES, timeout=ENGINE)
    expect(page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    return g.state()["game"]


def restored(g: Gowui, game: dict, screenshot: str) -> None:
    """The board after the restart: same moves, engine back, analysis recomputed."""
    page = g.page
    expect(page.locator("#move-counter")).to_have_text("2 / 2")
    assert g.state()["game"]["moves"] == game["moves"]
    assert g.state()["game"]["stones"] == game["stones"]
    expect(page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    expect(page.locator("#analysis-on")).to_be_checked()
    g.expect_dataset("candidates", CANDIDATES, timeout=ENGINE)
    expect(page.locator("#winbar-label")).to_have_text(re.compile(r"^black \d"), timeout=ENGINE)
    SCREENSHOTS.mkdir(parents=True, exist_ok=True)
    page.set_viewport_size({"width": 1000, "height": 680})
    g.expect_dataset("candidates", CANDIDATES, timeout=ENGINE)
    page.screenshot(path=str(SCREENSHOTS / screenshot))


def test_local_mode_restores_the_board_after_a_restart(start_engine, start_app, open_page,
                                                        tmp_path):
    engine = start_engine("gtp")
    state = tmp_path / "state.json"
    app = start_app(engine, True, "--state", str(state), fresh=False)
    g = open_page(app).open()
    game = play_and_analyse(g)

    g.page.close()
    app.process.stop()                           # shutdown saves the space (§8.2)
    assert state.is_file()

    again = start_app(engine, False, "--state", str(state), fresh=False)
    restored(open_page(again).open(), game, "local-mode.png")


def sign_in(open_page, app, name: str, password: str) -> Gowui:
    g = open_page(app)
    g.page.goto(app.url + "/")
    expect(g.page).to_have_url(re.compile(r"/login$"))
    g.page.locator("input[name=name]").fill(name)
    g.page.locator("input[name=password]").fill(password)
    g.page.locator("button[type=submit]").click()
    return g.ready()


def test_server_mode_restores_the_board_after_a_restart(start_engine, start_server_app,
                                                         open_page):
    engine = start_engine("gtp")
    catalog = [{"id": "kata", "label": "Fake KataGo", "protocol": "gtp",
                "host": "127.0.0.1", "port": engine.port}]
    app = start_server_app(engines=catalog, users={"alice": "password one"})
    g = sign_in(open_page, app, "alice", "password one")
    g.page.locator("#connect").click()
    game = play_and_analyse(g)
    assert g.wait_sent("connect", 0) == {"type": "connect", "engineId": "kata"}

    g.page.close()
    app.process.stop()                           # shutdown saves every space (§8.2)

    again = start_server_app(engines=catalog)    # the same database: the account is kept
    g = sign_in(open_page, again, "alice", "password one")
    expect(g.page.locator("#me-name")).to_have_text("alice")
    restored(g, game, "server-mode.png")
