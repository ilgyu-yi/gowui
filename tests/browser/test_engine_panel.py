"""The engine form, capability gating and the console (SPEC §3.8 "Engine form", "Capability
gating", "Controls"; baseline features B15 console, B17 connect / disconnect, B18 engine
parameters, B30 traffic log).
"""

from __future__ import annotations

import json
import re

import pytest

from browser_kit import ENGINE, expect

pytestmark = pytest.mark.browser

PROTOCOLS = ["gtp", "analysis", "handol"]
#: A sent line (``▸``) carrying the GTP ``version`` command (with its command id).
SENT_VERSION = re.compile(r"▸\s*(?:\d+\s+)?version")


def form(g) -> list[str]:
    return [g.page.locator(f"#{i}").input_value() for i in ("protocol", "host", "port")]


def console_section(g):
    return g.page.locator("details:has(#raw-form)")


def health_with(g, **changes) -> None:
    """Serve /api/health with ``changes`` applied to the real answer."""
    g.page.route("**/api/health", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**route.fetch().json(), **changes})))


def connected(start_engine, start_app, open_page, protocol: str):
    engine = start_engine(protocol)
    g = open_page(start_app(engine, True)).open()
    expect(g.page.locator("#connect")).to_have_text(g.t("disconnect"), timeout=ENGINE)
    return g, engine


# -- the form's fields -----------------------------------------------------------------------------
def test_the_form_shows_the_health_defaults_without_a_request(start_engine, start_app, open_page):
    engine = start_engine("analysis")
    g = open_page(start_app(engine)).open()
    expect(g.page.locator("#port")).to_have_value(str(engine.port))
    assert form(g) == ["analysis", "127.0.0.1", str(engine.port)]


def test_the_form_shows_the_page_defaults_when_health_cannot_be_read(start_app, open_page):
    g = open_page(start_app(None, False, "--engine-port", "7777"))
    g.allow_console(r"Failed to load resource|ERR_FAILED")
    g.page.route("**/api/health", lambda route: route.abort())
    g.open()
    assert form(g) == ["gtp", "127.0.0.1", "6363"]


def test_the_form_shows_the_accepted_request_while_connected(start_engine, start_app, open_page):
    g, engine = connected(start_engine, start_app, open_page, "gtp")
    assert form(g) == ["gtp", "127.0.0.1", str(engine.port)]


def test_the_form_shows_the_last_request_after_disconnect_and_reload(start_engine, start_app,
                                                                   open_page):
    """The request echo wins over the health defaults (``--engine-port 7777``) whether or not
    an engine is connected."""
    engine = start_engine("gtp")
    g = open_page(start_app(None, False, "--engine-port", "7777")).open()
    expect(g.page.locator("#port")).to_have_value("7777")
    g.page.locator("#port").fill(str(engine.port))
    g.page.locator("#connect").click()
    expect(g.page.locator("#connect")).to_have_text(g.t("disconnect"), timeout=ENGINE)
    g.page.locator("#connect").click()
    expect(g.page.locator("#connect")).to_have_text(g.t("connect"), timeout=ENGINE)
    g.page.reload()
    g.ready()
    expect(g.page.locator("#port")).to_have_value(str(engine.port))


@pytest.mark.parametrize("protocol, port", [("analysis", "6364"), ("handol", "11985"),
                                            ("gtp", "6363")])
def test_changing_the_protocol_sets_its_conventional_port(start_app, open_page, protocol, port):
    g = open_page(start_app()).open()
    g.page.locator("#port").fill("1234")
    g.page.locator("#protocol").select_option(protocol)
    expect(g.page.locator("#port")).to_have_value(port)


def test_a_state_broadcast_does_not_overwrite_what_the_user_typed(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#host").fill("typed.example")
    g.act({"type": "state"})
    expect(g.page.locator("#host")).to_have_value("typed.example")


def test_connect_sends_the_trimmed_host_and_an_integer_port(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#protocol").select_option("analysis")
    g.page.locator("#host").fill("  127.0.0.1  ")
    g.page.locator("#port").fill("6399")
    since = g.mark()
    g.page.locator("#connect").click()
    assert g.wait_sent("connect", since) == {"type": "connect", "protocol": "analysis",
                                             "host": "127.0.0.1", "port": 6399}


# -- connecting and the badge ----------------------------------------------------------------------------
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_connecting_shows_the_engines_name_in_the_badge(start_engine, start_app, open_page,
                                                        protocol):
    g, _ = connected(start_engine, start_app, open_page, protocol)
    name = g.state()["engine"]["name"]
    assert name
    expect(g.page.locator("#engine-state")).to_contain_text(name)


def test_the_badge_reads_disconnected_without_an_engine(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#engine-state")).to_have_text(g.t("disconnected"))


def test_the_connect_button_reads_connect_without_an_engine(start_app, open_page):
    g = open_page(start_app()).open()
    expect(g.page.locator("#connect")).to_have_text(g.t("connect"))


def test_disconnect_sends_disconnect(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    since = g.mark()
    g.page.locator("#connect").click()
    assert g.wait_sent("disconnect", since) == {"type": "disconnect"}


def test_disconnecting_sets_the_badge_back(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    g.page.locator("#connect").click()
    expect(g.page.locator("#engine-state")).to_have_text(g.t("disconnected"), timeout=ENGINE)


def test_the_badge_reads_thinking_while_the_engine_thinks(start_app, open_page):
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    state = dict(g.state())
    g.inject({**state, "thinking": True})
    expect(g.page.locator("#engine-state")).to_have_text(g.t("thinking"))


# -- capability gating --------------------------------------------------------------------------------
@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_engine_move_now_is_enabled_for_every_protocol(start_engine, start_app, open_page,
                                                       protocol):
    g, _ = connected(start_engine, start_app, open_page, protocol)
    expect(g.page.locator("#genmove")).to_be_enabled()


@pytest.mark.parametrize("protocol, enabled", [("gtp", True), ("analysis", False),
                                               ("handol", False)])
def test_final_score_is_enabled_only_where_the_engine_supports_it(start_engine, start_app,
                                                                  open_page, protocol, enabled):
    g, _ = connected(start_engine, start_app, open_page, protocol)
    button = expect(g.page.locator("#final-score"))
    button.to_be_enabled() if enabled else button.to_be_disabled()


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_console_field_follows_the_engines_console_flag(start_engine, start_app, open_page,
                                                            protocol):
    g, _ = connected(start_engine, start_app, open_page, protocol)
    field = expect(g.page.locator("#raw"))
    field.to_be_enabled() if g.state()["engine"]["console"] else field.to_be_disabled()


def test_the_gtp_console_is_offered(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    expect(g.page.locator("#raw")).to_be_enabled()


def test_the_capabilities_follow_the_flags_not_the_protocol(start_app, open_page):
    """An engine that says ``supportsFinalScore`` gets the button, whatever its protocol."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    state = g.state()
    engine = {**state["engine"], "connected": True, "protocol": "analysis", "name": "X",
              "version": "1", "supportsGenmove": True, "supportsFinalScore": True,
              "console": True}
    g.inject({**state, "engine": engine})
    expect(g.page.locator("#final-score")).to_be_enabled()


def test_the_console_section_is_hidden_when_health_says_no_console(start_app, open_page):
    g = open_page(start_app())
    health_with(g, console=False)
    g.open()
    expect(console_section(g)).to_be_hidden()


def test_the_console_section_is_shown_when_health_says_console(start_app, open_page):
    g = open_page(start_app()).open()
    expect(console_section(g)).to_be_visible()


def test_the_console_section_stays_when_health_cannot_be_read(start_app, open_page):
    g = open_page(start_app())
    g.allow_console(r"Failed to load resource")
    g.page.route("**/api/health", lambda route: route.fulfill(status=500, body="no"))
    g.open()
    expect(console_section(g)).to_be_visible()


# -- the console and the traffic log (B15, B30) -------------------------------------------------------------
def open_console(g) -> None:
    section = console_section(g)
    if section.get_attribute("open") is None:
        section.locator("> summary").click()


def test_the_console_sends_raw(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    open_console(g)
    since = g.mark()
    g.page.locator("#raw").fill("name")
    g.page.locator("#raw").press("Enter")
    assert g.wait_sent("raw", since) == {"type": "raw", "command": "name"}


def test_the_console_reply_appears_in_the_log(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    open_console(g)
    g.page.locator("#raw").fill("name")
    g.page.locator("#raw").press("Enter")
    name = g.state()["engine"]["name"]
    expect(g.page.locator("#log")).to_contain_text(name, timeout=ENGINE)


def test_a_sent_log_line_is_marked(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    open_console(g)
    g.page.locator("#raw").fill("version")
    g.page.locator("#raw").press("Enter")
    expect(g.page.locator("#log")).to_contain_text(SENT_VERSION, timeout=ENGINE)


def test_the_log_comes_back_after_a_reload(start_engine, start_app, open_page):
    g, _ = connected(start_engine, start_app, open_page, "gtp")
    open_console(g)
    g.page.locator("#raw").fill("version")
    g.page.locator("#raw").press("Enter")
    expect(g.page.locator("#log")).to_contain_text("version", timeout=ENGINE)
    g.page.reload()
    g.ready()
    open_console(g)
    expect(g.page.locator("#log")).to_contain_text(SENT_VERSION)


def test_the_page_keeps_at_most_400_log_lines(start_app, open_page):
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    for n in range(450):
        g.inject({"type": "log", "line": {"direction": "recv", "text": f"line {n}", "at": 0}})
    expect(g.page.locator("#log")).to_contain_text("line 449")
    assert g.page.evaluate("() => document.getElementById('log').childElementCount") == 400


def test_log_history_replaces_the_log(start_app, open_page):
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.inject({"type": "log", "line": {"direction": "recv", "text": "old line", "at": 0}})
    expect(g.page.locator("#log")).to_contain_text("old line")
    g.inject({"type": "log_history", "lines": [{"direction": "recv", "text": "new line", "at": 0}]})
    expect(g.page.locator("#log")).to_have_text(re.compile(r"^\s*new line\s*$"))


def test_engine_output_in_the_log_is_shown_as_text(start_app, open_page):
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.inject({"type": "log", "line": {"direction": "recv", "text": "<img src=x>", "at": 0}})
    expect(g.page.locator("#log")).to_contain_text("<img src=x>")
    expect(g.page.locator("#log img")).to_have_count(0)


# -- engine parameters (B18) ---------------------------------------------------------------------------------
def test_changing_visits_sends_max_visits(start_app, open_page):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator("#max-visits").fill("321")
    g.page.locator("#max-visits").press("Tab")
    assert g.wait_sent("engine_params", since)["maxVisits"] == 321


def test_changing_every_sends_the_report_interval(start_app, open_page):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator("#interval").fill("0.7")
    g.page.locator("#interval").press("Tab")
    assert g.wait_sent("engine_params", since)["reportInterval"] == 0.7


def test_the_visits_field_follows_the_server(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "engine_params", "maxVisits": 4321})
    expect(g.page.locator("#max-visits")).to_have_value("4321")


def test_a_focused_echoed_field_is_not_overwritten(start_app, open_page):
    g = open_page(start_app()).open()
    g.page.locator("#max-visits").fill("777")
    g.act({"type": "engine_params", "maxVisits": 4321})
    expect(g.page.locator("#max-visits")).to_have_value("777")


def test_continuous_analysis_sends_analysis(start_app, open_page):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator("#analysis-on").check()
    assert g.wait_sent("analysis", since) == {"type": "analysis", "enabled": True}


def test_the_analysis_box_follows_the_server(start_app, open_page):
    g = open_page(start_app()).open()
    g.act({"type": "analysis", "enabled": True})
    expect(g.page.locator("#analysis-on")).to_be_checked()
