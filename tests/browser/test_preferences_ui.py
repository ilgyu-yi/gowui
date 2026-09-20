"""The page's side of per-account preferences (SPEC §3.8 "Preferences", §4.1, §4.2, §8.4, §8.5;
issue #23 AC1 and AC2).

Server mode: the language and a tuple preset chosen in one browser are there in a second browser
signed in to the same account, and neither `localStorage` key is written. Local mode: the `state`
frame carries no preferences, and both go into this browser's storage, as they always have.

The page holds the bounds of §4.1 and §7.6 itself, because `preferences` is refused whole: an
over-long name is refused here and never wedges the next save, and a refusal the server makes all
the same puts the preset menu back to what the account holds.

The human policy panel — where presets are saved — is shown while the engine form's protocol is
`handol` (§3.8), so the server-mode catalog here holds a handol entry and the local-mode page
picks `handol` in the protocol select. Neither test connects to an engine.
"""

from __future__ import annotations

import json
import re

import pytest

from browser_kit import Gowui, expect

pytestmark = pytest.mark.browser

HANDOL = [{"id": "handol", "label": "Handol", "protocol": "handol",
           "host": "127.0.0.1", "port": 11985}]


def sign_in(open_page, app, name: str = "alice", password: str = "password one",
            g: Gowui | None = None) -> Gowui:
    g = open_page(app) if g is None else g
    g.page.goto(app.url + "/")
    expect(g.page).to_have_url(re.compile(r"/login$"))
    g.page.locator("input[name=name]").fill(name)
    g.page.locator("input[name=password]").fill(password)
    g.page.locator("button[type=submit]").click()
    return g.ready()


def stored(g: Gowui, key: str):
    return g.page.evaluate("(k) => localStorage.getItem(k)", key)


def save_preset(g: Gowui, name: str, builtin: str = "builtin:sharp") -> None:
    g.page.locator("#human-preset").select_option(builtin)
    g.page.once("dialog", lambda dialog: dialog.accept(name))
    g.page.locator("#preset-save").click()
    expect(g.page.locator("#human-preset")).to_have_value("user:" + name)


def test_an_account_finds_its_language_and_presets_in_a_second_browser(start_server_app,
                                                                        open_page):
    app = start_server_app(engines=HANDOL, users={"alice": "password one"})
    first = sign_in(open_page, app)
    expect(first.page.locator("#human-preset")).to_be_visible()

    since = first.mark()
    save_preset(first, "mine")
    sent = first.wait_sent("preferences", since)
    assert sent["presets"] == [{"name": "mine", "tuple": {"temperature": 0.5}}]

    since = first.mark()
    first.page.locator("#lang").select_option("ko")
    expect(first.page.locator("html")).to_have_attribute("lang", "ko")
    assert first.wait_sent("preferences", since)["lang"] == "ko"

    # The account holds them, so this browser holds neither (§8.5).
    first.fence()
    assert (stored(first, "gowui.lang"), stored(first, "gowui.userPresets")) == (None, None)
    assert first.state()["preferences"] == {
        "lang": "ko", "presets": [{"name": "mine", "tuple": {"temperature": 0.5}}]}

    # A second browser: a fresh context, so a fresh localStorage.
    second = sign_in(open_page, app)
    expect(second.page.locator("html")).to_have_attribute("lang", "ko")
    expect(second.page.locator("#human-preset option[value='user:mine']")).to_have_count(1)
    assert stored(second, "gowui.userPresets") is None


def test_a_name_the_account_would_refuse_is_refused_here_and_wedges_nothing(start_server_app,
                                                                            open_page):
    """§3.8 "Preferences": `preferences` is refused whole, so the page holds the caps of §7.6
    itself. One over-long name must not block every later save of the list."""
    app = start_server_app(engines=HANDOL, users={"alice": "password one"})
    g = sign_in(open_page, app)
    expect(g.page.locator("#human-preset")).to_be_visible()

    long_name = "x" * 45
    since = g.mark()
    g.page.locator("#human-preset").select_option("builtin:sharp")
    g.page.once("dialog", lambda dialog: dialog.accept(long_name))
    g.page.locator("#preset-save").click()
    g.fence()
    assert g.sent(since, "preferences") == [], "an over-long name was sent"
    # The page's own refusal, not the server's: nothing was sent to be refused.
    expect(g.page.locator("#status.error")).to_contain_text("40")
    expect(g.page.locator("#human-preset option[value='user:" + long_name + "']")).to_have_count(0)

    # The next, valid preset still reaches the account.
    since = g.mark()
    save_preset(g, "mine", "builtin:flat")
    assert g.wait_sent("preferences", since)["presets"] == [
        {"name": "mine", "tuple": {"temperature": 1.5}}]
    g.fence()
    assert g.state()["preferences"] == {
        "lang": None, "presets": [{"name": "mine", "tuple": {"temperature": 1.5}}]}


REFUSAL = "refused, for this test"


def refuse_preferences(g: Gowui) -> None:
    """Route the page's socket, dropping every `preferences` on the way out and answering the
    `error` a refusal brings (§4.1). Call before `goto`.

    The page now holds the same bounds as the server (above), so nothing it sends of its own is
    refused any more; the refusal is staged here to pin what the page does with one.
    """
    def handler(route):
        server = route.connect_to_server()

        def outgoing(message):
            if json.loads(message).get("type") == "preferences":
                route.send(json.dumps({"type": "error", "message": REFUSAL}))
                return
            server.send(message)

        route.on_message(outgoing)
        server.on_message(lambda message: route.send(message))

    g.page.route_web_socket(re.compile(r".*/ws$"), handler)


def test_a_refusal_puts_the_preset_menu_back_to_what_the_account_holds(start_server_app,
                                                                       open_page):
    """§3.8: a refusal brings no new `state`, so the page must put the menu back itself."""
    app = start_server_app(engines=HANDOL, users={"alice": "password one"})
    g = open_page(app)
    refuse_preferences(g)
    sign_in(open_page, app, g=g)
    expect(g.page.locator("#human-preset")).to_be_visible()

    g.page.locator("#human-preset").select_option("builtin:flat")
    g.page.once("dialog", lambda dialog: dialog.accept("ghost"))
    g.page.locator("#preset-save").click()
    expect(g.page.locator("#status.error")).to_have_text(REFUSAL)

    expect(g.page.locator("#human-preset option[value='user:ghost']")).to_have_count(0)
    assert g.state()["preferences"] == {"lang": None, "presets": []}


def test_local_mode_keeps_the_language_and_the_presets_in_the_browser(start_app, open_page):
    g = open_page(start_app()).open()
    assert g.state()["preferences"] is None

    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("html")).to_have_attribute("lang", "ko")
    assert stored(g, "gowui.lang") == "ko"

    g.page.locator("#protocol").select_option("handol")
    expect(g.page.locator("#human-preset")).to_be_visible()
    save_preset(g, "mine")
    assert json.loads(stored(g, "gowui.userPresets")) == [
        {"name": "mine", "tuple": {"temperature": 0.5}}]
    g.fence()
    assert g.sent(0, "preferences") == []
