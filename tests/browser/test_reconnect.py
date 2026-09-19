"""The connection (SPEC §3.8 "Connection"; §4.3 close codes).

The page's timers run on Playwright's fake clock, paused from the start, so "reconnects after
0.5 s" and "does not reconnect" are checked by advancing the clock, never by sleeping. Closes are
provoked through a WebSocket route (``4401`` / ``4403`` stop reconnecting and say why), or for
real: ``1009`` is the server refusing a frame over 1 MiB (§7.6). A route can only close with 1000
or 3000-4999 (Playwright mocks the socket in the page), so "any other code" is checked with 1000,
4000 and the real 1009; ``1013`` (queue overflow) has no cheap trigger and is left to the rule.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from browser_kit import expect, post_sgf

pytestmark = pytest.mark.browser

START = datetime(2030, 1, 1, 12, 0, 0)


OTHER_CODES = [1000, 1009, 4000]


def paused(start_app, open_page, *, proxy: bool = True):
    app = start_app()
    g = open_page(app)
    g.page.clock.install(time=START)
    g.page.clock.pause_at(START + timedelta(seconds=1))
    if proxy:
        g.proxy_ws()
    g.open()
    return g, app


def sockets(g) -> int:
    return g.page.evaluate("() => window.__gowuiTest.sockets.length")


def wait_sockets(g, count: int) -> None:
    g.until("(n) => window.__gowuiTest.sockets.length >= n", arg=count)


def close(g, code: int) -> None:
    """The server side closes the page's socket with ``code`` (1009: for real, by sending a
    frame over the 1 MiB limit)."""
    before = len(g.closes())
    if code == 1009:
        g.send_raw({"type": "state", "pad": "x" * (1024 * 1024 + 16)})
    else:
        g.routes[-1].close(code=code, reason="test")
    g.until("(n) => window.__gowuiTest.frames.filter((f) => f.dir === 'close')"
            ".length > n", arg=before)


# -- 4401 / 4403: stop and say why ----------------------------------------------------------------------
@pytest.mark.parametrize("code, key", [(4401, "status.notSignedIn"), (4403, "status.refused")])
def test_a_refusal_close_says_why(start_app, open_page, code, key):
    g, _ = paused(start_app, open_page)
    close(g, code)
    expect(g.page.locator("#status")).to_have_text(g.t(key))


@pytest.mark.parametrize("code", [4401, 4403])
def test_a_refusal_close_does_not_reconnect(start_app, open_page, code):
    g, _ = paused(start_app, open_page)
    close(g, code)
    g.page.clock.run_for(30_000)
    g.page.evaluate("() => 0")
    assert sockets(g) == 1


@pytest.mark.parametrize("code, key", [(4401, "status.notSignedIn"), (4403, "status.refused")])
def test_a_refusal_reason_stays_until_the_page_is_reloaded(start_app, open_page, code, key):
    g, _ = paused(start_app, open_page)
    close(g, code)
    g.page.clock.run_for(30_000)
    expect(g.page.locator("#status")).to_have_text(g.t(key))


def test_a_refused_handshake_says_not_signed_in(start_app, open_page):
    """The server accepts, then closes with 4401 at once (§4.3): the page never gets a state."""
    g = open_page(start_app())
    g.page.clock.install(time=START)
    g.page.clock.pause_at(START + timedelta(seconds=1))
    held = []
    g.page.route_web_socket(re.compile(r".*/ws$"), held.append)   # never reaches the server
    g.goto()
    g.until("() => window.__gowuiTest.sockets.length > 0")
    held[0].close(code=4401)
    expect(g.page.locator("#status")).to_have_text(g.t("status.notSignedIn"))


def test_a_frame_sent_after_a_refusal_is_dropped_and_the_reason_shown(start_app, open_page):
    g, _ = paused(start_app, open_page)
    close(g, 4403)
    since = g.mark()
    g.page.locator("#pass").click()
    expect(g.page.locator("#status")).to_have_text(g.t("status.refused"))
    assert g.sent(since) == []


# -- other closes: reconnect ---------------------------------------------------------------------------------
@pytest.mark.parametrize("code", OTHER_CODES)
def test_another_close_shows_the_lost_connection_text(start_app, open_page, code):
    g, _ = paused(start_app, open_page, proxy=code != 1009)
    close(g, code)
    expect(g.page.locator("#status")).to_have_text(
        "Lost the connection to gowui; reconnecting...")


@pytest.mark.parametrize("code", OTHER_CODES)
def test_another_close_reconnects_after_half_a_second(start_app, open_page, code):
    g, _ = paused(start_app, open_page, proxy=code != 1009)
    close(g, code)
    g.page.clock.run_for(499)
    g.page.evaluate("() => 0")
    assert sockets(g) == 1
    g.page.clock.run_for(2)
    wait_sockets(g, 2)


def test_opening_the_socket_again_clears_the_status(start_app, open_page):
    g, _ = paused(start_app, open_page)
    close(g, 1000)
    expect(g.page.locator("#status")).to_have_text(g.t("status.lost"))
    g.page.clock.run_for(501)
    wait_sockets(g, 2)
    expect(g.page.locator("#status")).to_have_text("")


def test_the_attach_frames_bring_the_page_up_to_date_after_a_reconnect(start_app, open_page):
    from session_helpers import sgf_of

    g, app = paused(start_app, open_page)
    close(g, 1000)
    post_sgf(app.url, sgf_of(9, "E5", "C3"))
    g.page.clock.run_for(501)
    wait_sockets(g, 2)
    expect(g.page.locator("#move-counter")).to_have_text("2 / 2")


def test_an_opened_socket_resets_the_reconnect_delay(start_app, open_page):
    g, _ = paused(start_app, open_page)
    close(g, 1000)
    g.page.clock.run_for(501)
    wait_sockets(g, 2)
    expect(g.page.locator("#status")).to_have_text("")
    close(g, 1000)
    g.page.clock.run_for(499)
    g.page.evaluate("() => 0")
    assert sockets(g) == 2
    g.page.clock.run_for(2)
    wait_sockets(g, 3)


def test_a_frame_sent_while_closed_is_dropped_not_queued(start_app, open_page):
    g, _ = paused(start_app, open_page)
    close(g, 1000)
    since = g.mark()
    g.page.locator("#pass").click()
    g.page.clock.run_for(501)
    wait_sockets(g, 2)
    expect(g.page.locator("#status")).to_have_text("")
    g.fence()
    assert g.sent(since) == []


def test_a_frame_sent_while_closed_shows_the_lost_connection_text(start_app, open_page):
    g, _ = paused(start_app, open_page)
    close(g, 1000)
    g.page.evaluate("() => { document.getElementById('status').textContent = ''; }")
    g.page.locator("#pass").click()
    expect(g.page.locator("#status")).to_have_text(g.t("status.lost"))


def test_failed_tries_double_the_delay_up_to_eight_seconds(start_app, open_page):
    """With the app gone, each try fails without opening: 0.5, 1, 2, 4, 8, then 8 s again."""
    g, app = paused(start_app, open_page, proxy=False)
    g.allow_console(r"WebSocket connection to .* failed")
    app.process.stop()
    g.until("() => window.__gowuiTest.frames.some((f) => f.dir === 'close')")
    count = 1
    for delay in [500, 1000, 2000, 4000, 8000, 8000]:
        g.page.clock.run_for(delay - 1)
        g.page.evaluate("() => 0")
        assert sockets(g) == count, f"reconnected before {delay} ms"
        g.page.clock.run_for(1)
        count += 1
        wait_sockets(g, count)
        g.until(
            "(n) => window.__gowuiTest.frames.filter((f) => f.dir === 'close').length >= n",
            arg=count)
