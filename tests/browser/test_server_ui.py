"""Browser smoke run of server mode (SPEC §3.8 "Server-mode additions", §7.11; issue #9): the page
sends a visitor without a session to the sign-in page, signing in shows the board with the
engine picker in place of the address fields and the signed-in name, the picker connects to the
catalog engine, and Log out returns to the sign-in page — with no console error or CSP violation.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import ENGINE, Gowui, expect

pytestmark = pytest.mark.browser


def test_sign_in_then_the_board_with_the_catalog_picker(start_engine, start_server_app,
                                                          open_page):
    engine = start_engine("gtp")
    app = start_server_app(
        engines=[{"id": "kata", "label": "Fake KataGo", "protocol": "gtp",
                  "host": "127.0.0.1", "port": engine.port}],
        users={"alice": "password one"})
    g: Gowui = open_page(app, locale="en-US")
    page = g.page
    page.goto(app.url + "/")
    expect(page).to_have_url(re.compile(r"/login$"))
    expect(page.locator("html")).to_have_attribute("lang", "en")
    g.screenshot("server-sign-in")

    page.locator("input[name=name]").fill("alice")
    page.locator("input[name=password]").fill("password one")
    page.locator("button[type=submit]").click()
    g.ready()

    expect(page.locator("#engine-pick")).to_be_visible()
    expect(page.locator("#engine-pick option")).to_have_text(["Fake KataGo"])
    expect(page.locator("#host")).to_be_hidden()
    expect(page.locator("#port")).to_be_hidden()
    expect(page.locator("#protocol")).to_be_hidden()
    expect(page.locator("#me-name")).to_have_text("alice")
    expect(page.locator("#logout-form")).to_be_visible()

    page.locator("#connect").click()
    expect(page.locator("#engine-state")).not_to_have_text(re.compile("disconnected"),
                                                            timeout=ENGINE)
    assert g.wait_sent("connect", 0) == {"type": "connect", "engineId": "kata"}
    g.screenshot("server-board")

    g.allow_console(r"WebSocket|4401")
    page.locator("#logout-form button").click()
    expect(page).to_have_url(re.compile(r"/login$"))
