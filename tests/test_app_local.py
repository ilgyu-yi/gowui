"""The web app in local mode (SPEC §3.1, §4, §5, §6.1–§6.3, §9): one app factory fed a policy
bundle, one route module, a WebSocket that drives the session against the fake engine, the HTTP
routes and their answers, and the lifespan startup.

Every test runs a real uvicorn server on port 0 (tests/app_helpers.py); the harness API the
implementer builds to is listed in that module's docstring.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import re
import threading
import warnings
from pathlib import Path

import pytest

from app_helpers import LOOPBACK, MIB, local_bundle, snapshot_with
from helpers import HANG, wait_for
from session_helpers import gtp_count, settle, sgf_of

REPO = Path(__file__).resolve().parent.parent
PACKAGE = REPO / "gowui"
ROUTE_DECORATOR = re.compile(r"@(app|router)\.(get|post|put|delete|websocket)")


async def ready_tab(tabs, running):
    """A tab that has received its attach frames."""
    tab = await tabs(running)
    assert not isinstance(tab, int), f"the handshake was refused with HTTP {tab}"
    assert await tab.wait_state() is not None, "no state frame on attach"
    return tab


async def play(tab, *vertices: str) -> dict:
    """Play vertices for the side to move and wait for the state showing them."""
    frame = tab.state()
    for vertex in vertices:
        start = tab.mark()
        cursor = frame["game"]["cursor"]
        await tab.send({"type": "play", "color": frame["game"]["toPlay"], "vertex": vertex})
        frame = await tab.wait_state(lambda f, c=cursor: f["game"]["cursor"] == c + 1, start)
        assert frame is not None, f"{vertex} was not played: {tab.of('error', start)}"
    return frame


async def new_game(tab, size: int = 9) -> dict:
    start = tab.mark()
    await tab.send({"type": "new_game", "size": size, "komi": None, "rules": "japanese",
                    "handicap": 0})
    frame = await tab.wait_state(lambda f: f["game"]["size"] == size
                                 and f["game"]["moveCount"] == 0, start)
    assert frame is not None, "no state for the new game"
    return frame


# -- one factory, policies as data (§6.1) --------------------------------------------------------
def test_the_app_factory_takes_the_policy_bundle_and_startup_hooks_only():
    from gowui.app import create_app

    assert list(inspect.signature(create_app).parameters) == ["policies", "startup"]


def test_the_policy_bundle_has_the_fields_of_section_6_1():
    from gowui.policies import Policies

    assert [f.name for f in dataclasses.fields(Policies)] == [
        "identity", "engines", "storage", "idle_release_seconds", "allowed_hosts",
        "allow_ip_literals", "trusted_proxies", "sign_in_url"]


def test_an_identity_has_the_fields_of_section_6_2():
    from gowui.policies import Identity

    assert [f.name for f in dataclasses.fields(Identity)] == [
        "key", "name", "source", "logout_kind", "logout_url"]


def test_an_identity_is_frozen():
    from gowui.policies import Identity

    identity = Identity(key="owner", name="", source="none", logout_kind="", logout_url="")
    with pytest.raises(dataclasses.FrozenInstanceError):
        identity.key = "other"  # type: ignore[misc]


def test_the_local_identity_policy_identifies_every_request_as_the_owner():
    from gowui.policies import Identity

    assert local_bundle().identity.identify(object()) == Identity(
        key="owner", name="", source="none", logout_kind="", logout_url="")


def test_the_local_bundle_never_releases_a_space():
    assert local_bundle().idle_release_seconds is None


def test_the_local_bundle_allows_the_loopback_names_and_the_bound_host():
    assert local_bundle(host="127.0.0.1").allowed_hosts == frozenset(
        {"localhost", "127.0.0.1", "::1"})


def test_the_local_bundle_adds_the_normalised_host_flag_to_the_allowed_hosts():
    assert local_bundle(host="MyBox.LAN").allowed_hosts == frozenset(
        {"localhost", "127.0.0.1", "::1", "mybox.lan"})


def test_the_local_bundle_allows_ip_literals():
    assert local_bundle().allow_ip_literals is True


def test_the_local_bundle_trusts_no_proxy():
    assert local_bundle().trusted_proxies == ()


def test_the_local_bundle_has_no_sign_in_url():
    assert local_bundle().sign_in_url is None


def test_the_local_bundle_uses_typed_engine_addresses():
    from gowui.local_mode import TypedAddresses

    assert isinstance(local_bundle().engines, TypedAddresses)


# -- AC4: one route module; lifespan, not on_event (§5, §9) ----------------------------------------
def test_exactly_one_module_under_gowui_declares_routes_and_it_is_routes_py():
    declaring = sorted(str(path.relative_to(REPO)) for path in PACKAGE.rglob("*.py")
                       if ROUTE_DECORATOR.search(path.read_text(encoding="utf-8")))
    assert declaring == ["gowui/routes.py"]


def test_no_module_under_gowui_uses_on_event():
    using = [str(path.relative_to(REPO)) for path in PACKAGE.rglob("*.py")
             if "on_event" in path.read_text(encoding="utf-8")]
    assert using == []


async def test_starting_and_stopping_the_app_raises_no_on_event_deprecation_warning(serve):
    from gowui.app import create_app

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        running = await serve(create_app(local_bundle()))
        await running.stop()
    assert [str(w.message) for w in caught if "on_event" in str(w.message)] == []


async def test_startup_hooks_are_awaited_with_the_app_before_the_server_listens(serve):
    from gowui.app import create_app

    calls = []

    async def hook(app):
        calls.append(app)

    app = create_app(local_bundle(), startup=[hook])
    await serve(app)
    assert calls == [app]


# -- AC1: typed connect → play → analysis over the WebSocket (§4, §6.3) ---------------------------
@pytest.mark.parametrize("protocol", ["gtp", "analysis", "handol"])
async def test_a_tab_connects_by_typed_address_plays_and_receives_an_analysis_report(
        local_app, tabs, fake_engine, protocol):
    server = await fake_engine(protocol)
    tab = await ready_tab(tabs, local_app)
    await new_game(tab)
    start = tab.mark()
    await tab.send({"type": "connect", "protocol": protocol, "host": LOOPBACK,
                    "port": server.port})
    connected = await tab.wait_state(lambda f: f["engine"]["connected"], start)
    assert connected is not None, f"not connected: {tab.of('error', start)}"
    await play(tab, "D4", "F6")
    start = tab.mark()
    await tab.send({"type": "analysis", "enabled": True})
    frame = await tab.wait("analysis", lambda f: f["cursor"] == 2, start)
    assert frame is not None and frame["analysis"]["moveInfos"], tab.of("error", start)


# -- tabs share one space (§3.1, §4.2, §4.3) ------------------------------------------------------
async def test_a_tab_is_sent_state_then_log_history_on_attach(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    await wait_for(lambda: len(tab.frames) >= 2)
    assert [f["type"] for f in tab.frames[:2]] == ["state", "log_history"]


async def test_a_move_played_in_one_tab_reaches_the_other_tab(local_app, tabs):
    first = await ready_tab(tabs, local_app)
    second = await ready_tab(tabs, local_app)
    start = second.mark()
    await play(first, "D4")
    frame = await second.wait_state(lambda f: f["game"]["moveCount"] == 1, start)
    assert frame is not None


async def test_a_tab_opened_later_is_brought_up_to_date_by_its_attach_frames(local_app, tabs):
    first = await ready_tab(tabs, local_app)
    await play(first, "D4", "E5")
    later = await ready_tab(tabs, local_app)
    assert later.of("state")[0]["game"]["moveCount"] == 2


async def test_a_state_message_gets_a_fresh_state(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    start = tab.mark()
    await tab.send({"type": "state"})
    assert await tab.wait_state(start=start) is not None


# -- /healthz and /api/health (§5) -------------------------------------------------------------
async def test_healthz_answers_ok(local_app):
    async with local_app.client() as client:
        response = await client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


async def test_api_health_has_exactly_the_keys_of_section_5(local_app):
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert sorted(body) == sorted(["ok", "engine", "rules", "ruleDefaults", "handicapKomi",
                                   "engineAddress", "console", "me"])


async def test_api_health_lists_the_rule_sets_sorted_with_their_default_komi(local_app):
    from gowui.rules import RULE_SETS

    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert (body["rules"], body["ruleDefaults"]) == (
        sorted(RULE_SETS), {name: rs.default_komi for name, rs in RULE_SETS.items()})


async def test_api_health_reports_the_handicap_komi(local_app):
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert body["handicapKomi"] == 0.5


async def test_api_health_describes_the_typed_policy_with_its_defaults(serve):
    from gowui.app import create_app

    running = await serve(create_app(local_bundle(engine_defaults={
        "protocol": "analysis", "host": "10.0.0.2", "port": 7000})))
    async with running.client() as client:
        body = (await client.get("/api/health")).json()
    assert body["engineAddress"] == {"kind": "typed", "defaults": {
        "protocol": "analysis", "host": "10.0.0.2", "port": 7000}}


async def test_api_health_says_the_local_policy_offers_the_console(local_app):
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert body["console"] is True


async def test_api_health_reports_the_local_owner_as_me(local_app):
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert body["me"] == {"name": "", "source": "none", "logout": "", "logoutUrl": ""}


async def test_api_health_engine_is_the_engine_object_of_state(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert body["engine"] == tab.state()["engine"]


async def test_api_health_engine_shows_a_connected_engine(local_app, tabs, gtp_server):
    tab = await ready_tab(tabs, local_app)
    start = tab.mark()
    await tab.send({"type": "connect", "protocol": "gtp", "host": LOOPBACK,
                    "port": gtp_server.port})
    assert await tab.wait_state(lambda f: f["engine"]["connected"], start) is not None
    async with local_app.client() as client:
        body = (await client.get("/api/health")).json()
    assert (body["engine"]["connected"], body["engine"]["request"]) == (
        True, {"protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})


# -- GET /api/sgf (§5) ---------------------------------------------------------------------------
async def test_get_sgf_downloads_the_active_board(local_app, tabs):
    from gowui.game import Game

    tab = await ready_tab(tabs, local_app)
    await new_game(tab)
    await play(tab, "D4", "E5", "F6")
    async with local_app.client() as client:
        response = await client.get("/api/sgf")
    assert Game.from_sgf(response.text).move_count == 3


async def test_get_sgf_is_an_sgf_attachment_named_gowui_sgf(local_app):
    async with local_app.client() as client:
        response = await client.get("/api/sgf")
    assert (response.status_code, response.headers["content-type"],
            response.headers["content-disposition"]) == (
        200, "application/x-go-sgf; charset=utf-8", 'attachment; filename="gowui.sgf"')


async def test_get_sgf_writes_a_lone_surrogate_as_a_question_mark(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    start = tab.mark()
    # json.dumps escapes the lone surrogate as \ud800, so the frame is valid UTF-8 text
    await tab.send(json.dumps({"type": "load_sgf", "sgf": "(;GM[1]SZ[9]PB[x\ud800y];B[ee])"}))
    assert await tab.wait_state(lambda f: f["game"]["moveCount"] == 1, start) is not None
    async with local_app.client() as client:
        response = await client.get("/api/sgf")
    assert (response.status_code, b"PB[x?y]" in response.content) == (200, True)


# -- POST /api/sgf (§5) ----------------------------------------------------------------------------
async def test_post_sgf_loads_the_body_into_the_active_board(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4", "E5"))
    assert (response.status_code, response.json()) == (200, {"ok": True, "moves": 2})


async def test_post_sgf_broadcasts_the_new_state_to_every_tab(local_app, tabs):
    first = await ready_tab(tabs, local_app)
    second = await ready_tab(tabs, local_app)
    starts = first.mark(), second.mark()
    async with local_app.client() as client:
        await client.post("/api/sgf", content=sgf_of(9, "D4", "E5", "F6"))
    frames = [await tab.wait_state(lambda f: f["game"]["moveCount"] == 3, start)
              for tab, start in zip((first, second), starts)]
    assert all(frame is not None for frame in frames)


@pytest.mark.parametrize("content_type", ["text/plain", "application/json",
                                          "application/x-go-sgf", None])
async def test_post_sgf_reads_the_body_whatever_its_content_type(local_app, content_type):
    headers = {"Content-Type": content_type} if content_type else {}
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"), headers=headers)
    assert response.status_code == 200


@pytest.mark.parametrize("body", [b"", b"   \r\n\t  "])
async def test_post_sgf_refuses_an_empty_body_with_400(local_app, body):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=body)
    assert (response.status_code, "error" in response.json()) == (400, True)


async def test_post_sgf_refuses_text_that_is_not_sgf_with_400(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content="this is not an sgf")
    assert (response.status_code, isinstance(response.json().get("error"), str)) == (400, True)


async def test_a_refused_upload_leaves_the_board_unchanged(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    await new_game(tab)
    await play(tab, "D4")
    async with local_app.client() as client:
        await client.post("/api/sgf", content="(;GM[1]SZ[99])")
    await settle(0.3)
    assert tab.state()["game"]["moveCount"] == 1


async def test_the_session_keeps_working_after_a_refused_upload(local_app):
    async with local_app.client() as client:
        await client.post("/api/sgf", content="(;GM[1]SZ[99])")
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"))
    assert response.json() == {"ok": True, "moves": 1}


async def test_post_sgf_decodes_bytes_that_are_not_utf8_with_replacement(local_app):
    body = b"(;GM[1]SZ[9]PB[caf\xe9];B[ee])"
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=body)
    assert response.json() == {"ok": True, "moves": 1}


async def test_post_sgf_parses_the_upload_off_the_event_loop(local_app, monkeypatch):
    from gowui.game import Game

    threads = []
    real = Game.from_sgf.__func__

    def recording(cls, text, *args, **kwargs):
        threads.append(threading.current_thread())
        return real(cls, text, *args, **kwargs)

    monkeypatch.setattr(Game, "from_sgf", classmethod(recording))
    async with local_app.client() as client:
        await client.post("/api/sgf", content=sgf_of(9, "D4"))
    assert threads and threading.main_thread() not in threads


async def test_post_sgf_of_exactly_one_mib_is_accepted(local_app):
    head, tail = "(;GM[1]SZ[9];B[ee]C[", "])"
    body = (head + "a" * (MIB - len(head) - len(tail)) + tail).encode()
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=body)
    assert (len(body), response.status_code) == (MIB, 200)


# -- no generated API docs; local sign-in routes (§5) --------------------------------------------
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"])
async def test_there_is_no_generated_api_documentation(local_app, path):
    async with local_app.client() as client:
        response = await client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("method,path", [("GET", "/login"), ("POST", "/login"),
                                         ("POST", "/logout")])
async def test_the_local_sign_in_routes_redirect_to_the_page(local_app, method, path):
    async with local_app.client() as client:
        response = await client.request(method, path, headers={"Origin": local_app.origin})
    assert (response.status_code, response.headers.get("location")) == (303, "/")


async def test_the_page_is_served_at_the_root(local_app):
    """Needs the placeholder ``gowui/static/index.html`` (#8 replaces it)."""
    async with local_app.client() as client:
        response = await client.get("/")
    assert (response.status_code, response.headers["content-type"].split(";")[0]) == (
        200, "text/html")


# -- lifespan startup through the CLI (§8.1, §9) ----------------------------------------------------
async def test_connect_flag_connects_to_the_engine_flags_at_startup(serve, tabs, gtp_server):
    from gowui.cli import build_app, parse

    app = build_app(parse(["--fresh", "--connect", "--engine-protocol", "gtp",
                           "--engine-host", LOOPBACK, "--engine-port", str(gtp_server.port)]))
    running = await serve(app)
    tab = await ready_tab(tabs, running)
    assert await tab.wait_state(lambda f: f["engine"]["connected"]) is not None


async def test_a_restored_connected_space_reconnects_at_startup(serve, tabs, gtp_server,
                                                                 tmp_path):
    from gowui.cli import build_app, parse

    state = tmp_path / "state.json"
    state.write_text(json.dumps(snapshot_with("D4", connected=True, request={
        "protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})))
    running = await serve(build_app(parse(["--state", str(state)])))
    tab = await ready_tab(tabs, running)
    assert await tab.wait_state(lambda f: f["engine"]["connected"]) is not None


async def test_connect_flag_does_not_double_a_restored_reconnect(serve, tabs, gtp_server,
                                                                 tmp_path):
    from gowui.cli import build_app, parse

    state = tmp_path / "state.json"
    state.write_text(json.dumps(snapshot_with("D4", connected=True, request={
        "protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})))
    running = await serve(build_app(parse([
        "--state", str(state), "--connect", "--engine-protocol", "gtp",
        "--engine-host", LOOPBACK, "--engine-port", str(gtp_server.port)])))
    tab = await ready_tab(tabs, running)
    await tab.wait_state(lambda f: f["engine"]["connected"])
    await settle(0.5)
    assert gtp_count(gtp_server, "list_commands") == 1


async def test_the_owner_space_is_loaded_before_the_first_request(serve, tmp_path):
    from gowui.cli import build_app, parse

    state = tmp_path / "state.json"
    state.write_text(json.dumps(snapshot_with("D4")))
    app = build_app(parse(["--state", str(state)]))
    await serve(app)
    assert list(app.state.registry.live) == ["owner"]


async def test_a_failing_session_command_never_closes_the_socket(local_app, tabs):
    tab = await ready_tab(tabs, local_app)
    start = tab.mark()
    await tab.send({"type": "play", "color": "black", "vertex": "ZZ99"})
    await tab.send({"type": "no such type"})
    await tab.send({"type": "state"})
    frame = await tab.wait_state(start=start, timeout=HANG)
    assert (frame is not None, len(tab.of("error", start)) >= 2) == (True, True)
