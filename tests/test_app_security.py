"""The guard and the transport limits (SPEC §4.3, §5, §7.4–§7.6): duplicate headers, the Host rule
and its normaliser, the Origin rule, the identity gate with exact public routes, fixed refusal
text, the response headers on every answer, and the size limits of uploads and frames.

Header shapes a client library will not send — two ``Host`` headers, no ``Host`` — go straight to
``gowui.guard.Guard`` in a hand-built ASGI scope; everything else goes through a real server.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json

import pytest

from app_helpers import (CSP, LOOPBACK, MIB, call_guard, http_status, local_bundle, raw_http,
                         refusal_code, scope_for, without_identity, ws_outcome)
from helpers import HANG
from session_helpers import settle, sgf_of

EVIL = "evil.example"


async def ready(tabs, running, **kwargs):
    tab = await tabs(running, **kwargs)
    assert not isinstance(tab, int), f"the handshake was refused with HTTP {tab}"
    return tab


async def stays_open(tabs, running, **kwargs) -> bool:
    """Whether a tab opened with these handshake headers gets its state and stays open."""
    tab = await ready(tabs, running, **kwargs)
    return await tab.wait_state() is not None and tab.open


# -- duplicate headers (§7.4 rule 1), through the guard directly ----------------------------------
async def test_two_host_headers_are_refused_with_403():
    sent, _ = await call_guard(local_bundle(), scope_for(headers=[
        ("Host", "127.0.0.1:8080"), ("Host", "127.0.0.1:8080")]))
    assert http_status(sent) == 403


async def test_two_host_headers_on_a_handshake_are_accepted_then_closed_with_4403():
    sent, _ = await call_guard(local_bundle(), scope_for("websocket", path="/ws", headers=[
        ("Host", "127.0.0.1:8080"), ("Host", "127.0.0.1:8080")]))
    assert ws_outcome(sent) == ("accept", 4403)


async def test_two_origin_headers_are_refused_even_when_both_match():
    sent, _ = await call_guard(local_bundle(), scope_for(method="POST", path="/api/sgf", headers=[
        ("Host", "127.0.0.1:8080"), ("Origin", "http://127.0.0.1:8080"),
        ("Origin", "http://127.0.0.1:8080")]))
    assert http_status(sent) == 403


async def test_two_x_forwarded_host_headers_are_refused():
    sent, _ = await call_guard(local_bundle(), scope_for(headers=[
        ("Host", "127.0.0.1:8080"), ("X-Forwarded-Host", "127.0.0.1"),
        ("X-Forwarded-Host", "127.0.0.1")]))
    assert http_status(sent) == 403


async def test_a_refused_request_never_reaches_the_app():
    _, inner = await call_guard(local_bundle(), scope_for(headers=[
        ("Host", "127.0.0.1:8080"), ("Host", "127.0.0.1:8080")]))
    assert inner.reached == 0


# -- the Host rule (§7.4 rule 2) -------------------------------------------------------------------
async def test_a_request_without_host_is_refused_with_403():
    sent, _ = await call_guard(local_bundle(), scope_for(headers=[]))
    assert http_status(sent) == 403


async def test_a_handshake_without_host_is_closed_with_4403():
    sent, _ = await call_guard(local_bundle(), scope_for("websocket", path="/ws", headers=[]))
    assert ws_outcome(sent) == ("accept", 4403)


async def test_an_http_1_0_request_without_host_is_refused_by_the_real_server(local_app):
    status, _, _ = await raw_http(local_app.port, b"GET /api/health HTTP/1.0\r\n\r\n")
    assert status == 403


@pytest.mark.parametrize("host", [
    "", "a@127.0.0.1", "127.0.0.1/x", "127.0.0.1\\x", "127.0.0.1 ", "local host", "127.0.0.1\x01",
    "127.0.0.1:0", "127.0.0.1:65536", "127.0.0.1:+80", "127.0.0.1:８０", "127.0.0.1:80a",
    "::1", "[localhost]", "[127.0.0.1]", "[::1", "127.1", "0x7f.1", "0177.0.0.1", EVIL,
    "localhost.evil.example", "evil.example:8080",
])
async def test_the_host_normaliser_and_allow_list_refuse(host):
    sent, _ = await call_guard(local_bundle(), scope_for(headers=[("Host", host)]))
    assert http_status(sent) == 403


@pytest.mark.parametrize("host", [
    "127.0.0.1", "127.0.0.1:8080", "localhost", "LOCALHOST:8080", "[::1]", "[::1]:8080",
    "10.0.0.5:80", "192.168.1.20:8080", "[fe80::1]:1", "127.0.0.1:1", "127.0.0.1:65535",
    "127.9.9.9:8080",
])
async def test_the_local_host_rule_accepts_loopback_names_and_ip_literals(host):
    sent, _ = await call_guard(local_bundle(), scope_for(headers=[("Host", host)]))
    assert http_status(sent) == 200


async def test_the_local_host_rule_accepts_the_bound_host_name_in_any_case():
    sent, _ = await call_guard(local_bundle(host="mybox.lan"),
                               scope_for(headers=[("Host", "MyBox.LAN:8080")]))
    assert http_status(sent) == 200


async def test_a_bundle_without_an_allow_list_accepts_any_host():
    sent, _ = await call_guard(dataclasses.replace(local_bundle(), allowed_hosts=None),
                               scope_for(headers=[("Host", EVIL)]))
    assert http_status(sent) == 200


async def test_ip_literals_do_not_pass_when_the_bundle_disallows_them():
    policies = dataclasses.replace(local_bundle(), allow_ip_literals=False)
    sent, _ = await call_guard(policies, scope_for(headers=[("Host", "10.0.0.5:8080")]))
    assert http_status(sent) == 403


@pytest.mark.parametrize("path", ["/", "/api/health", "/api/sgf", "/healthz", "/nowhere",
                                  "/css/site.css"])
async def test_a_foreign_host_is_refused_with_403_on_every_route(local_app, path):
    async with local_app.client() as client:
        response = await client.get(path, headers={"Host": f"{EVIL}:{local_app.port}"})
    assert response.status_code == 403


async def test_a_host_with_userinfo_is_refused_by_the_real_server(local_app):
    async with local_app.client() as client:
        response = await client.get("/api/health", headers={"Host": "a@127.0.0.1"})
    assert response.status_code == 403


async def test_a_foreign_host_handshake_is_closed_with_4403(local_app):
    assert await refusal_code(local_app, host=f"{EVIL}:{local_app.port}") == 4403


@pytest.mark.parametrize("host", ["localhost:{port}", "127.0.0.1", "[::1]:{port}",
                                  "192.168.1.20:{port}"])
async def test_loopback_names_and_ip_literals_reach_the_app(local_app, host):
    async with local_app.client() as client:
        response = await client.get("/api/health",
                                    headers={"Host": host.format(port=local_app.port)})
    assert response.status_code == 200


async def test_the_bound_host_name_reaches_the_app(serve):
    from gowui.app import create_app

    running = await serve(create_app(local_bundle(host="mybox.lan")))
    async with running.client() as client:
        response = await client.get("/api/health", headers={"Host": f"mybox.lan:{running.port}"})
    assert response.status_code == 200


async def test_forwarded_host_from_an_untrusted_peer_cannot_make_a_foreign_host_pass(local_app):
    async with local_app.client() as client:
        response = await client.get("/api/health", headers={
            "Host": f"{EVIL}:{local_app.port}", "X-Forwarded-Host": "127.0.0.1"})
    assert response.status_code == 403


async def test_forwarded_host_from_an_untrusted_peer_is_ignored(local_app):
    async with local_app.client() as client:
        response = await client.get("/api/health", headers={
            "Host": local_app.host, "X-Forwarded-Host": EVIL})
    assert response.status_code == 200


def proxied_bundle():
    """A bundle that trusts the loopback peer as a proxy and allows only ``app.example``."""
    return dataclasses.replace(local_bundle(), allowed_hosts=frozenset({"app.example"}),
                               allow_ip_literals=False,
                               trusted_proxies=(ipaddress.ip_network("127.0.0.0/8"),))


async def test_forwarded_host_from_a_trusted_proxy_is_the_effective_host():
    sent, _ = await call_guard(proxied_bundle(), scope_for(headers=[
        ("Host", "127.0.0.1:8080"), ("X-Forwarded-Host", "app.example")]))
    assert http_status(sent) == 200


async def test_forwarded_host_from_a_trusted_proxy_is_checked_against_the_allow_list():
    sent, _ = await call_guard(proxied_bundle(), scope_for(headers=[
        ("Host", "app.example"), ("X-Forwarded-Host", EVIL)]))
    assert http_status(sent) == 403


async def test_forwarded_host_from_a_peer_outside_the_trusted_proxies_is_ignored():
    sent, _ = await call_guard(proxied_bundle(), scope_for(client="10.9.9.9", headers=[
        ("Host", "127.0.0.1:8080"), ("X-Forwarded-Host", "app.example")]))
    assert http_status(sent) == 403


async def test_origin_is_matched_against_the_forwarded_host_from_a_trusted_proxy():
    sent, _ = await call_guard(proxied_bundle(), scope_for(method="POST", headers=[
        ("Host", "127.0.0.1:8080"), ("X-Forwarded-Host", "app.example"),
        ("Origin", "http://app.example")]))
    assert http_status(sent) == 200


# -- the Origin rule (§7.4 rule 3) ---------------------------------------------------------------
@pytest.mark.parametrize("origin", [
    f"http://{EVIL}", "null", "http://localhost:{port}", "http://127.0.0.1:1",
    "ftp://127.0.0.1:{port}", "http://127.0.0.1", "https://127.0.0.1", "chrome-extension://abc",
])
async def test_a_handshake_from_another_origin_is_closed_with_4403(local_app, origin):
    assert await refusal_code(local_app, origin=origin.format(port=local_app.port)) == 4403


async def test_a_handshake_with_two_origin_headers_is_closed_with_4403(local_app):
    assert await refusal_code(local_app, headers=[("Origin", local_app.origin),
                                                  ("Origin", local_app.origin)]) == 4403


async def test_a_same_origin_handshake_is_accepted(local_app, tabs):
    assert await stays_open(tabs, local_app, origin=local_app.origin)


async def test_a_handshake_without_origin_is_accepted(local_app, tabs):
    assert await stays_open(tabs, local_app)


async def test_a_same_origin_handshake_on_localhost_is_accepted(local_app, tabs):
    assert await stays_open(tabs, local_app, host=f"localhost:{local_app.port}",
                            origin=f"http://localhost:{local_app.port}")


@pytest.mark.parametrize("origin", [
    f"http://{EVIL}", "null", "http://localhost:{port}", "http://127.0.0.1:1",
    "ftp://127.0.0.1:{port}", "http://127.0.0.1",
])
async def test_a_cross_origin_upload_is_refused_with_403(local_app, origin):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"), headers={
            "Origin": origin.format(port=local_app.port)})
    assert response.status_code == 403


async def test_a_cross_origin_upload_leaves_the_board_unchanged(local_app, tabs):
    tab = await ready(tabs, local_app)
    await tab.wait_state()
    async with local_app.client() as client:
        await client.post("/api/sgf", content=sgf_of(9, "D4", "E5"),
                          headers={"Origin": f"http://{EVIL}"})
        response = await client.get("/api/sgf")
    await settle(0.2)
    assert (b";B[" in response.content, tab.state()["game"]["moveCount"]) == (False, 0)


async def test_an_upload_with_two_origin_headers_is_refused_with_403(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"), headers=[
            ("Origin", local_app.origin), ("Origin", local_app.origin)])
    assert response.status_code == 403


async def test_a_same_origin_upload_is_accepted(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"),
                                     headers={"Origin": local_app.origin})
    assert response.status_code == 200


async def test_an_upload_without_origin_is_accepted(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"))
    assert response.status_code == 200


async def test_an_https_origin_with_the_same_host_and_port_matches():
    """The match compares host and port, not scheme (§7.4)."""
    sent, _ = await call_guard(local_bundle(), scope_for(method="POST", headers=[
        ("Host", "127.0.0.1:8080"), ("Origin", "https://127.0.0.1:8080")]))
    assert http_status(sent) == 200


async def test_the_origin_host_is_compared_case_insensitively(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"), headers={
            "Host": f"localhost:{local_app.port}",
            "Origin": f"http://LocalHost:{local_app.port}"})
    assert response.status_code == 200


async def test_a_missing_port_is_the_scheme_default_on_both_sides():
    sent, _ = await call_guard(local_bundle(), scope_for(method="POST", headers=[
        ("Host", "127.0.0.1"), ("Origin", "http://127.0.0.1")]))
    assert http_status(sent) == 200


async def test_an_explicit_default_port_matches_an_omitted_one():
    sent, _ = await call_guard(local_bundle(), scope_for(method="POST", headers=[
        ("Host", "127.0.0.1:80"), ("Origin", "http://127.0.0.1")]))
    assert http_status(sent) == 200


async def test_a_cross_origin_get_is_not_subject_to_the_origin_rule(local_app):
    async with local_app.client() as client:
        response = await client.get("/api/health", headers={"Origin": f"http://{EVIL}"})
    assert response.status_code == 200


@pytest.mark.parametrize("path", ["/login", "/logout"])
async def test_a_cross_origin_post_to_the_sign_in_routes_is_refused(local_app, path):
    async with local_app.client() as client:
        response = await client.post(path, headers={"Origin": f"http://{EVIL}"})
    assert response.status_code == 403


# -- refusal text (§7.4) -------------------------------------------------------------------------
async def test_a_host_refusal_does_not_echo_the_host_or_the_path(local_app):
    async with local_app.client() as client:
        response = await client.get("/secret-path-xyz", headers={
            "Host": f"{EVIL}:{local_app.port}"})
    assert (EVIL in response.text, "secret-path-xyz" in response.text) == (False, False)


async def test_an_origin_refusal_does_not_echo_the_origin(local_app):
    async with local_app.client() as client:
        response = await client.post("/api/sgf", content="x",
                                     headers={"Origin": "http://origin-marker.example"})
    assert "origin-marker" not in response.text


# -- identity and public routes (§5, §6.2, §7.4 rule 4) -----------------------------------------
@pytest.fixture
async def anonymous_app(serve):
    """An app whose identity policy identifies nobody, with no sign-in URL."""
    from gowui.app import create_app

    return await serve(create_app(without_identity(local_bundle())))


@pytest.mark.parametrize("path", ["/api/health", "/api/sgf"])
async def test_without_an_identity_an_api_route_answers_401_json(anonymous_app, path):
    async with anonymous_app.client() as client:
        response = await client.get(path)
    assert (response.status_code, response.headers["content-type"].split(";")[0]) == (
        401, "application/json")


async def test_without_an_identity_an_upload_answers_401(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.post("/api/sgf", content=sgf_of(9, "D4"))
    assert response.status_code == 401


async def test_without_an_identity_the_websocket_closes_with_4401(anonymous_app):
    assert await refusal_code(anonymous_app) == 4401


async def test_without_an_identity_or_sign_in_url_a_page_answers_401(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/")
    assert response.status_code == 401


async def test_without_an_identity_a_page_goes_to_the_sign_in_url(serve):
    from gowui.app import create_app

    running = await serve(create_app(without_identity(local_bundle(), sign_in_url="/login")))
    async with running.client() as client:
        response = await client.get("/")
    assert (response.status_code in (302, 303, 307), response.headers.get("location")) == (
        True, "/login")


async def test_a_sign_in_url_does_not_turn_an_api_401_into_a_redirect(serve):
    from gowui.app import create_app

    running = await serve(create_app(without_identity(local_bundle(), sign_in_url="/login")))
    async with running.client() as client:
        response = await client.get("/api/health")
    assert response.status_code == 401


async def test_healthz_needs_no_identity(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/healthz")
    assert (response.status_code, response.json()) == (200, {"ok": True})


async def test_the_sign_in_page_needs_no_identity(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/login")
    assert response.text == "sign in"


async def test_a_stylesheet_path_needs_no_identity(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/css/no-such-sheet.css")
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/healthzX", "/healthz/", "/login/x", "/logoutX", "/cssx",
                                  "/css"])
async def test_a_path_that_only_resembles_a_public_route_needs_an_identity(anonymous_app, path):
    async with anonymous_app.client() as client:
        response = await client.get(path)
    assert response.status_code == 401


@pytest.mark.parametrize("target", [
    "/css/../api/sgf", "/css/%2e%2e/api/health", "/css/%2E%2E/api/health", "/css/a%2fb.css",
    "/css/a%2Fb.css", "/css/..", "/css/sub/../../api/sgf",
])
async def test_a_stylesheet_path_that_escapes_the_prefix_needs_an_identity(anonymous_app, target):
    request = (f"GET {target} HTTP/1.1\r\nHost: {anonymous_app.host}\r\n"
               "Connection: close\r\n\r\n").encode()
    status, _, _ = await raw_http(anonymous_app.port, request)
    assert status == 401


async def test_the_host_rule_applies_to_the_public_routes(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/healthz", headers={"Host": EVIL})
    assert response.status_code == 403


# -- response headers on every answer (§7.5) ------------------------------------------------------
async def answers(running) -> dict[str, dict]:
    """One response of each kind the app gives, by name: its lower-cased headers."""
    out: dict[str, dict] = {}
    async with running.client() as client:
        out["200 api"] = dict((await client.get("/api/health")).headers)
        out["200 healthz"] = dict((await client.get("/healthz")).headers)
        out["200 page"] = dict((await client.get("/")).headers)
        out["200 sgf"] = dict((await client.get("/api/sgf")).headers)
        out["400"] = dict((await client.post("/api/sgf", content=b"")).headers)
        out["403"] = dict((await client.get("/", headers={"Host": EVIL})).headers)
        out["404"] = dict((await client.get("/no/such/path")).headers)
        out["303"] = dict((await client.get("/login")).headers)
    request = (f"POST /api/sgf HTTP/1.1\r\nHost: {running.host}\r\nContent-Length: {2 * MIB}\r\n"
               "Connection: close\r\n\r\n").encode()
    _, out["413"], _ = await raw_http(running.port, request)
    return out


@pytest.mark.parametrize("kind", ["200 api", "200 healthz", "200 page", "200 sgf", "400", "403",
                                  "404", "303", "413"])
async def test_every_answer_carries_the_content_security_policy(local_app, kind):
    headers = (await answers(local_app))[kind]
    assert headers.get("content-security-policy") == CSP


@pytest.mark.parametrize("kind", ["200 api", "200 healthz", "200 page", "200 sgf", "400", "403",
                                  "404", "303", "413"])
async def test_every_answer_carries_nosniff(local_app, kind):
    headers = (await answers(local_app))[kind]
    assert headers.get("x-content-type-options") == "nosniff"


async def test_a_401_carries_the_security_headers(anonymous_app):
    async with anonymous_app.client() as client:
        response = await client.get("/api/health")
    assert (response.headers.get("content-security-policy"),
            response.headers.get("x-content-type-options")) == (CSP, "nosniff")


# -- upload size (§5, §7.6) ----------------------------------------------------------------------
def upload_head(running, extra: str) -> bytes:
    return (f"POST /api/sgf HTTP/1.1\r\nHost: {running.host}\r\n{extra}"
            "Connection: close\r\n\r\n").encode()


async def test_an_upload_declaring_more_than_one_mib_is_refused_before_it_is_read(local_app):
    status, _, _ = await raw_http(local_app.port,
                                  upload_head(local_app, f"Content-Length: {MIB + 1}\r\n"))
    assert status == 413


async def test_an_upload_of_one_mib_and_one_byte_is_refused_with_413(local_app):
    body = b"(" + b"a" * MIB
    status, _, _ = await raw_http(local_app.port, [
        upload_head(local_app, f"Content-Length: {len(body)}\r\n"),
        *[body[i:i + 65536] for i in range(0, len(body), 65536)]])
    assert status == 413


async def test_a_chunked_upload_over_one_mib_is_refused_with_413(local_app):
    piece = b"a" * 65536
    chunks = [f"{len(piece):x}\r\n".encode() + piece + b"\r\n"] * (MIB // len(piece) + 2)
    status, _, _ = await raw_http(local_app.port, [
        upload_head(local_app, "Transfer-Encoding: chunked\r\n"), *chunks, b"0\r\n\r\n"])
    assert status == 413


async def test_an_oversize_upload_leaves_the_board_unchanged(local_app, tabs):
    tab = await ready(tabs, local_app)
    await tab.wait_state()
    body = ("(;GM[1]SZ[9];B[ee]C[" + "a" * MIB + "])").encode()
    await raw_http(local_app.port, [upload_head(local_app, f"Content-Length: {len(body)}\r\n"),
                                    *[body[i:i + 65536] for i in range(0, len(body), 65536)]])
    await settle(0.2)
    assert tab.state()["game"]["moveCount"] == 0


# -- WebSocket frames (§4.3, §7.6) -----------------------------------------------------------------
async def test_a_frame_over_one_mib_closes_the_socket_with_1009(local_app, tabs):
    tab = await ready(tabs, local_app)
    await tab.wait_state()
    await tab.send(json.dumps({"type": "state", "pad": "a" * MIB}))
    assert await tab.close_code() == 1009


async def test_a_frame_of_exactly_one_mib_is_handled(local_app, tabs):
    tab = await ready(tabs, local_app)
    await tab.wait_state()
    head = '{"type": "state", "pad": "'
    frame = head + "a" * (MIB - len(head) - 2) + '"}'
    start = tab.mark()
    await tab.send(frame)
    assert (len(frame), await tab.wait_state(start=start) is not None) == (MIB, True)


@pytest.mark.parametrize("text", ["not json", "{", "[1, 2]", "42", '"text"', "null",
                                  "[" * 100_000 + "]" * 100_000,
                                  '{"a":' * 100_000 + "1" + "}" * 100_000])
async def test_a_frame_that_is_not_a_json_object_gets_an_error_and_the_socket_stays_open(
        local_app, tabs, text):
    tab = await ready(tabs, local_app)
    await tab.wait_state()
    start = tab.mark()
    await tab.send(text)
    error = await tab.wait("error", start=start)
    await tab.send({"type": "state"})
    after = await tab.wait_state(start=start, timeout=HANG)
    assert (error is not None, after is not None, tab.open) == (True, True, True)
