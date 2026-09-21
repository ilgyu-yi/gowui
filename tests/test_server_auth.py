"""Server mode: sign-in, sessions, throttling, SSO and the sign-in page (SPEC §5, §6.2, §7.1–§7.3,
§7.9–§7.11, §10; issue #9 AC3, AC4 and AC5).

Every test runs a real server over a temporary database. Password hashing is a low-cost scrypt
counted by ``CountingHasher`` (tests/server_helpers.py), so "no scrypt ran" is a number, and a
per-verification delay makes concurrent sign-ins really overlap.
"""

from __future__ import annotations

import asyncio

import pytest

from app_helpers import refusal_code
from helpers import HANG, wait_for
from server_helpers import (PASSWORD, CountingHasher, config, cookie_header, get,
                            location_error, login, start, ws_headers)


# -- AC4: sign-in, log-out, sessions ------------------------------------------------------------
async def test_sign_in_sets_a_fresh_http_only_cookie(serve, tmp_path):
    server = await start(serve, tmp_path)
    response, token = await login(server.running, "alice")
    assert (response.status_code, response.headers["location"]) == (303, "/")
    cookie = response.headers["set-cookie"]
    for flag in ("HttpOnly", "SameSite=Lax", "Path=/", f"Max-Age={14 * 86400}"):
        assert flag.lower() in cookie.lower(), flag
    assert "secure" not in cookie.lower()
    again, token2 = await login(server.running, "alice", headers=cookie_header(token))
    assert token2 and token2 != token


async def test_a_wrong_password_and_an_unknown_name_answer_alike(serve, tmp_path):
    server = await start(serve, tmp_path)
    wrong, token = await login(server.running, "alice", "not the password")
    unknown, token2 = await login(server.running, "nobody", PASSWORD)
    assert (wrong.status_code, location_error(wrong), token) == (303, "wrong", None)
    assert (unknown.status_code, unknown.headers["location"], token2) == \
        (303, wrong.headers["location"], None)
    assert server.hasher.verifications == 2  # the missing name ran scrypt too


async def test_signed_in_requests_reach_the_api_and_others_do_not(serve, tmp_path):
    server = await start(serve, tmp_path)
    _, token = await login(server.running, "alice")
    assert (await get(server.running, "/api/health", token)).status_code == 200
    assert (await get(server.running, "/api/health")).status_code == 401
    assert (await get(server.running, "/api/health", "forged")).status_code == 401
    page = await get(server.running, "/")
    assert (page.status_code, page.headers["location"]) == (303, "/login")


async def test_log_out_revokes_the_token_server_side(serve, tabs, tmp_path):
    server = await start(serve, tmp_path, revalidate=0.2)
    running = server.running
    _, token = await login(running, "alice")
    tab = await tabs(running, origin=running.origin, headers=ws_headers(token))
    await tab.wait_state()
    async with running.client() as client:
        out = await client.post("/logout", headers={"Host": running.host,
                                                    "Origin": running.origin,
                                                    **cookie_header(token)})
    assert (out.status_code, out.headers["location"]) == (303, "/login")
    assert "max-age=0" in out.headers["set-cookie"].lower()
    assert (await get(running, "/api/health", token)).status_code == 401
    assert await tab.close_code() == 4401


async def test_a_second_device_keeps_its_socket_after_the_first_logs_out(serve, tabs, tmp_path):
    server = await start(serve, tmp_path, revalidate=0.2)
    running = server.running
    _, first = await login(running, "alice")
    _, second = await login(running, "alice")
    tab1 = await tabs(running, origin=running.origin, headers=ws_headers(first))
    tab2 = await tabs(running, origin=running.origin, headers=ws_headers(second))
    await tab1.wait_state()
    await tab2.wait_state()
    async with running.client() as client:
        await client.post("/logout", headers={"Host": running.host, "Origin": running.origin,
                                              **cookie_header(first)})
    assert await tab1.close_code() == 4401
    await asyncio.sleep(0.6)  # three revalidation ticks
    assert tab2.open
    mark = tab2.mark()
    await tab2.send({"type": "state"})
    assert await tab2.wait_state(start=mark) is not None


async def test_a_password_change_ends_every_session(serve, tabs, tmp_path):
    server = await start(serve, tmp_path, revalidate=0.2)
    running = server.running
    _, token = await login(running, "alice")
    tab = await tabs(running, origin=running.origin, headers=ws_headers(token))
    await tab.wait_state()
    assert server.store.set_password("alice", "another password")
    assert (await get(running, "/api/health", token)).status_code == 401
    assert await tab.close_code() == 4401
    _, fresh = await login(running, "alice", "another password")
    assert fresh


async def test_a_sign_in_racing_a_password_change_gets_no_token(tmp_path):
    """§7.2 "No race": the token is stored only if the verified hash is still current."""
    from server_helpers import open_store

    store = open_store(tmp_path / "gowui.db")
    store.add_user("alice", PASSWORD)
    verified = store.check_password("alice", PASSWORD)
    store.set_password("alice", "another password")
    assert store.open_login(verified, 60) is None


# -- AC4: throttling ----------------------------------------------------------------------------
async def test_five_failures_throttle_even_the_right_password(serve, tmp_path):
    server = await start(serve, tmp_path)
    for _ in range(5):
        response, _ = await login(server.running, "alice", "wrong password")
        assert location_error(response) == "wrong"
    before = server.hasher.verifications
    response, token = await login(server.running, "alice")
    assert (location_error(response), token) == ("throttled", None)
    assert server.hasher.verifications == before


async def test_twenty_concurrent_wrong_sign_ins_run_scrypt_at_most_five_times(serve, tmp_path):
    hasher = CountingHasher(delay=0.2)
    server = await start(serve, tmp_path, hasher=hasher)
    running = server.running
    async with running.client() as client:
        responses = await asyncio.gather(*(
            login(running, "alice", f"wrong password {i}", client=client) for i in range(20)))
    errors = sorted(location_error(r) for r, _ in responses)
    assert hasher.verifications <= 5
    assert errors.count("throttled") >= 15


async def test_a_throttled_client_does_not_lock_out_another_behind_the_proxy(serve, tmp_path):
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    for _ in range(5):
        await login(server.running, "alice", "wrong password",
                    headers={"X-Forwarded-For": "203.0.113.7"})
    blocked, _ = await login(server.running, "alice", headers={"X-Forwarded-For": "203.0.113.7"})
    other, token = await login(server.running, "alice",
                               headers={"X-Forwarded-For": "198.51.100.9"})
    assert (location_error(blocked), other.headers["location"], bool(token)) == \
        ("throttled", "/", True)


async def test_addresses_in_one_ipv6_64_share_one_budget(serve, tmp_path):
    """§7.1: an IPv6 client is keyed by its /64, so rotating addresses in it buys nothing."""
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    for i in range(1, 6):
        await login(server.running, "alice", "wrong password",
                    headers={"X-Forwarded-For": f"2001:db8:1:2::{i:x}"})
    before = server.hasher.verifications
    same_64, token = await login(server.running, "alice",
                                 headers={"X-Forwarded-For": "2001:db8:1:2:ffff::99"})
    assert (location_error(same_64), token) == ("throttled", None)
    assert server.hasher.verifications == before
    other_64, token = await login(server.running, "alice",
                                  headers={"X-Forwarded-For": "2001:db8:1:3::1"})
    assert (other_64.headers["location"], bool(token)) == ("/", True)


async def test_the_per_name_cap_holds_across_many_clients(serve, tmp_path):
    """§7.1: 20 failures per name per 10 minutes, whatever the client address."""
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    for client in range(4):
        for _ in range(5):
            response, _ = await login(server.running, "alice", "wrong password",
                                      headers={"X-Forwarded-For": f"203.0.113.{client + 1}"})
            assert location_error(response) == "wrong"
    before = server.hasher.verifications
    fresh, token = await login(server.running, "alice",
                               headers={"X-Forwarded-For": "198.51.100.200"})
    assert (location_error(fresh), token) == ("throttled", None)
    assert server.hasher.verifications == before
    bob, token = await login(server.running, "bob", headers={"X-Forwarded-For": "198.51.100.200"})
    assert (bob.headers["location"], bool(token)) == ("/", True)


async def test_an_empty_password_takes_no_slot_and_runs_no_scrypt(serve, tmp_path):
    """§7.1: a malformed attempt is refused before the throttle."""
    server = await start(serve, tmp_path)
    before = server.hasher.verifications
    for _ in range(10):
        response, _ = await login(server.running, "alice", "")
        assert location_error(response) == "wrong"
    assert server.hasher.verifications == before
    response, token = await login(server.running, "alice")
    assert (response.headers["location"], bool(token)) == ("/", True)


# -- AC4: cross-site refusal ---------------------------------------------------------------------
async def test_a_cross_site_sign_in_is_refused(serve, tmp_path):
    server = await start(serve, tmp_path)
    response, token = await login(server.running, "alice",
                                  headers={"Origin": "http://evil.example"})
    assert (response.status_code, token) == (403, None)
    assert server.hasher.verifications == 0


async def test_a_cross_site_log_out_is_refused(serve, tmp_path):
    server = await start(serve, tmp_path)
    running = server.running
    _, token = await login(running, "alice")
    async with running.client() as client:
        out = await client.post("/logout", headers={"Host": running.host,
                                                    "Origin": "http://evil.example",
                                                    **cookie_header(token)})
    assert out.status_code == 403
    assert (await get(running, "/api/health", token)).status_code == 200


async def test_a_cross_site_websocket_with_a_valid_cookie_is_refused(serve, tmp_path):
    server = await start(serve, tmp_path)
    _, token = await login(server.running, "alice")
    code = await refusal_code(server.running, origin="http://evil.example",
                              headers=ws_headers(token))
    assert code == 4403


# -- AC3: the SSO header ------------------------------------------------------------------------
async def test_the_sso_header_is_believed_from_a_trusted_proxy(serve, tmp_path):
    server = await start(serve, tmp_path, auth="header", trusted_proxies="127.0.0.1/32",
                         logout_url="https://sso.example/logout")
    info = (await get(server.running, "/api/health", **{"X-authentik-username": "alice"})).json()
    assert info["me"] == {"name": "alice", "source": "sso", "logout": "sso",
                          "logoutUrl": "https://sso.example/logout"}


async def test_the_sso_header_is_ignored_from_an_untrusted_peer(serve, tmp_path):
    server = await start(serve, tmp_path, auth="local,header", trusted_proxies="10.0.0.0/8")
    response = await get(server.running, "/api/health", **{"X-authentik-username": "alice"})
    assert response.status_code == 401
    code = await refusal_code(server.running, origin=server.running.origin,
                              headers=[("X-authentik-username", "alice")])
    assert code == 4401


def test_header_auth_without_trusted_proxies_refuses_to_start(tmp_path):
    from gowui.server_mode import ConfigError

    with pytest.raises(ConfigError) as refused:
        config(tmp_path / "gowui.db", auth="header")
    assert refused.value.variable == "GOWUI_TRUSTED_PROXIES"


def test_serve_refuses_header_auth_without_proxies_before_binding(tmp_path, monkeypatch, capsys):
    from gowui.cli import main

    monkeypatch.setenv("GOWUI_DB", str(tmp_path / "gowui.db"))
    monkeypatch.setenv("GOWUI_AUTH", "header")
    monkeypatch.delenv("GOWUI_TRUSTED_PROXIES", raising=False)
    try:
        code = main(["serve", "--port", "0"])
    except SystemExit as exited:
        code = exited.code
    assert code == 2
    assert "GOWUI_TRUSTED_PROXIES" in capsys.readouterr().err
    assert not (tmp_path / "gowui.db").exists()


@pytest.mark.parametrize("variable,value", [
    ("auth", "ldap"), ("trusted_proxies", "10.0.0.1/8"), ("engines", "{}"),
    ("engines", '[{"id": "a", "protocol": "gtp", "host": "h", "port": true}]'),
    ("session_days", "nan"), ("idle_minutes", "0"), ("cookie_secure", "yes"),
    ("logout_url", "javascript:alert(1)"), ("auth_header", "bad header"),
])
def test_a_malformed_variable_refuses_the_start(tmp_path, variable, value):
    from gowui.server_mode import ConfigError

    with pytest.raises(ConfigError) as refused:
        config(tmp_path / "gowui.db", **{variable: value})
    assert refused.value.variable == f"GOWUI_{variable.upper()}"


# -- AC5: /api/health and the sign-in page ---------------------------------------------------------
async def test_health_reports_a_password_account_with_a_form_log_out(serve, tmp_path):
    server = await start(serve, tmp_path)
    _, token = await login(server.running, "alice")
    info = (await get(server.running, "/api/health", token)).json()
    assert info["me"] == {"name": "alice", "source": "local", "logout": "local", "logoutUrl": ""}
    assert info["engineAddress"] == {"kind": "catalog", "engines": []}
    assert info["console"] is False
    assert "mode" not in info


async def test_health_reports_an_sso_account_without_a_log_out_url(serve, tmp_path):
    server = await start(serve, tmp_path, auth="header", trusted_proxies="127.0.0.1/32")
    info = (await get(server.running, "/api/health", **{"X-authentik-username": "bo"})).json()
    assert info["me"] == {"name": "bo", "source": "sso", "logout": "", "logoutUrl": ""}


@pytest.mark.parametrize("query,accept,lang,marker", [
    ("", "ko-KR,ko;q=0.9", "ko", "로그인"),
    ("", "en-US,en;q=0.9", "en", "Sign in"),
    ("?lang=en", "ko-KR", "en", "Sign in"),
    ("?lang=ko&error=wrong", "en-US", "ko", "이름 또는 비밀번호"),
])
async def test_the_sign_in_page_speaks_korean_and_english(serve, tmp_path, query, accept, lang,
                                                          marker):
    server = await start(serve, tmp_path)
    page = await get(server.running, "/login" + query, **{"Accept-Language": accept})
    assert page.status_code == 200
    assert f'<html lang="{lang}">' in page.text
    assert marker in page.text
    assert "<script" not in page.text and "style=" not in page.text
    assert page.headers["content-security-policy"].startswith("default-src 'self'")
    assert (await get(server.running, "/css/signin.css")).status_code == 200


async def test_the_sign_in_page_echoes_no_request_value(serve, tmp_path):
    server = await start(serve, tmp_path)
    page = await get(server.running, "/login?error=%3Cb%3Ehello&lang=%3Ci%3E")
    assert "hello" not in page.text and "<b>" not in page.text and "<i>" not in page.text


async def test_a_sign_in_body_over_8_kib_is_refused(serve, tmp_path):
    server = await start(serve, tmp_path)
    running = server.running
    async with running.client() as client:
        response = await client.post("/login", content=b"name=alice&password=" + b"x" * 9000,
                                     headers={"Host": running.host,
                                              "Content-Type":
                                                  "application/x-www-form-urlencoded"})
    assert response.status_code == 413
    assert server.hasher.verifications == 0


async def test_header_only_auth_has_no_sign_in_page(serve, tmp_path):
    server = await start(serve, tmp_path, auth="header", trusted_proxies="10.0.0.0/8")
    login_page = await get(server.running, "/login")
    assert (login_page.status_code, login_page.headers.get("location")) == (303, "/")
    assert (await get(server.running, "/")).status_code == 401


async def test_revalidation_waits_for_its_tick(serve, tabs, tmp_path):
    """A CLI password change reaches an open socket on the next tick, not before (§4.3)."""
    server = await start(serve, tmp_path, revalidate=0.3)
    _, token = await login(server.running, "alice")
    tab = await tabs(server.running, origin=server.running.origin, headers=ws_headers(token))
    await tab.wait_state()
    server.store.remove_user("alice")
    await wait_for(lambda: not tab.open, HANG)
    assert tab.ws.close_code == 4401


# -- §7.10: forwarded headers from a trusted proxy (carry-over I4) -----------------------------------
@pytest.mark.parametrize("peer,trusted", [("::ffff:10.1.1.1", True), ("10.1.1.1", True),
                                          ("::ffff:10.2.0.1", False), ("not-an-ip", False)])
def test_an_ipv4_mapped_peer_matches_an_ipv4_network(peer, trusted):
    import ipaddress

    from gowui.guard import trusted_peer

    scope = {"client": (peer, 5000), "headers": []}
    assert trusted_peer(scope, (ipaddress.ip_network("10.1.0.0/16"),)) is trusted


@pytest.mark.parametrize("lines,https", [(["http, https"], True), (["https, http"], False),
                                         (["http", "HTTPS"], True), (["https", "http"], False)])
def test_only_the_last_forwarded_proto_element_counts(lines, https):
    import ipaddress

    from gowui.guard import request_is_https

    scope = {"client": ("10.1.1.1", 5000), "scheme": "http",
             "headers": [(b"x-forwarded-proto", line.encode()) for line in lines]}
    assert request_is_https(scope, (ipaddress.ip_network("10.1.0.0/16"),)) is https


def test_the_throttle_client_is_the_last_valid_forwarded_for_from_a_trusted_peer():
    import ipaddress

    from gowui.guard import client_address

    proxies = (ipaddress.ip_network("10.1.0.0/16"),)

    def scope(peer, xff):
        return {"client": (peer, 5000), "headers": [(b"x-forwarded-for", xff.encode())]}

    assert client_address(scope("10.1.1.1", "1.2.3.4, 5.6.7.8"), proxies) == "5.6.7.8"
    assert client_address(scope("10.1.1.1", "1.2.3.4, junk"), proxies) == "10.1.1.1"
    assert client_address(scope("192.0.2.9", "5.6.7.8"), proxies) == "192.0.2.9"


@pytest.mark.parametrize("lines,https", [(["wss"], True), (["WSS"], True), (["ws"], False),
                                         (["http, wss"], True), (["wss, http"], False)],
                         ids=["wss", "upper", "ws", "last-wss", "last-http"])
def test_a_forwarded_wss_counts_as_https(lines, https):
    """§7.10: the header carries a URL scheme, and a TLS-terminating proxy writes `wss` for a
    WebSocket upgrade. Reading only `https` leaves every socket behind such a proxy counting as
    plain, which the origin rule of §7.4 then refuses on the port."""
    import ipaddress

    from gowui.guard import request_is_https

    scope = {"client": ("10.1.1.1", 5000), "scheme": "ws",
             "headers": [(b"x-forwarded-proto", line.encode()) for line in lines]}
    assert request_is_https(scope, (ipaddress.ip_network("10.1.0.0/16"),)) is https


def test_an_untrusted_peers_forwarded_wss_is_still_ignored():
    """§7.10 reads a forwarded header only from a trusted peer, `wss` no differently."""
    import ipaddress

    from gowui.guard import request_is_https

    scope = {"client": ("192.0.2.9", 5000), "scheme": "ws",
             "headers": [(b"x-forwarded-proto", b"wss")]}
    assert request_is_https(scope, (ipaddress.ip_network("10.1.0.0/16"),)) is False


async def test_cookie_secure_follows_a_trusted_forwarded_proto(serve, tmp_path):
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    response, _ = await login(server.running, "alice", headers={"X-Forwarded-Proto": "https"})
    assert "secure" in response.headers["set-cookie"].lower()


async def test_cookie_secure_follows_a_trusted_forwarded_wss(serve, tmp_path):
    """The same rule, for the scheme a proxy writes on an upgrade (§7.10)."""
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    response, _ = await login(server.running, "alice", headers={"X-Forwarded-Proto": "wss"})
    assert "secure" in response.headers["set-cookie"].lower()


# -- §7.2: the cookie's name follows the Secure flag ---------------------------------------------------
HOST_COOKIE = "__Host-gowui_session"


def cookie_set(response) -> tuple[str, str]:
    """The name and the value of the cookie the answer sets."""
    name, _, rest = response.headers["set-cookie"].partition("=")
    return name, rest.split(";")[0]


async def test_the_cookie_takes_the_host_prefix_when_it_is_secure(serve, tmp_path):
    """§7.2: `__Host-` is a write restriction the browser enforces on everyone else, and it is
    granted only to a Secure cookie with `Path=/` and no `Domain`."""
    server = await start(serve, tmp_path, cookie_secure="1")
    response, _ = await login(server.running, "alice")
    cookie = response.headers["set-cookie"].lower()
    assert cookie_set(response)[0] == HOST_COOKIE
    assert "secure" in cookie and "path=/" in cookie and "domain=" not in cookie


async def test_the_cookie_keeps_its_plain_name_while_it_is_not_secure(serve, tmp_path):
    """§7.2: under `auto` over plain http the cookie is not Secure, so the prefix is not taken."""
    server = await start(serve, tmp_path)
    response, _ = await login(server.running, "alice")
    assert cookie_set(response)[0] == "gowui_session"


async def test_only_the_name_in_force_is_read(serve, tmp_path):
    """§7.2: there is no switch-over. While the `__Host-` name is in force a `gowui_session` a
    sibling origin planted is ignored, and the plain name ignores a `__Host-` one."""
    secure = await start(serve, tmp_path, cookie_secure="1")
    _, value = cookie_set((await login(secure.running, "alice"))[0])
    assert (await get(secure.running, "/api/health",
                      Cookie=f"{HOST_COOKIE}={value}")).status_code == 200
    assert (await get(secure.running, "/api/health",
                      Cookie=f"gowui_session={value}")).status_code == 401

    plain = await start(serve, tmp_path, db=tmp_path / "plain" / "gowui.db")
    _, other = cookie_set((await login(plain.running, "alice"))[0])
    assert (await get(plain.running, "/api/health",
                      Cookie=f"gowui_session={other}")).status_code == 200
    assert (await get(plain.running, "/api/health",
                      Cookie=f"{HOST_COOKIE}={other}")).status_code == 401


async def test_log_out_clears_the_cookie_in_force(serve, tmp_path):
    """§7.2: the cleared cookie carries the name and the flags the browser needs to accept it."""
    server = await start(serve, tmp_path, cookie_secure="1")
    running = server.running
    _, value = cookie_set((await login(running, "alice"))[0])
    async with running.client() as client:
        out = await client.post("/logout", headers={"Host": running.host,
                                                    "Origin": running.origin,
                                                    "Cookie": f"{HOST_COOKIE}={value}"})
    cleared = out.headers["set-cookie"]
    assert cookie_set(out)[0] == HOST_COOKIE
    assert "max-age=0" in cleared.lower() and "secure" in cleared.lower()
    assert (await get(running, "/api/health", Cookie=f"{HOST_COOKIE}={value}")).status_code == 401


# -- §7.1: the cost of a password hash ---------------------------------------------------------------
def test_the_stored_hash_uses_the_owasp_equivalent_cost():
    from gowui.store import Scrypt

    hasher = Scrypt()
    assert (hasher.n, hasher.r, hasher.p) == (2 ** 16, 8, 2)


def test_a_hash_written_with_an_older_cost_still_verifies():
    """§7.1: every hash carries its own parameters, so raising the cost keeps old hashes usable."""
    from gowui.store import Scrypt

    stored = Scrypt(n=2 ** 4, r=1, p=1).hash(PASSWORD)
    assert stored.startswith("scrypt$16$1$1$")
    assert Scrypt().verify(PASSWORD, stored) and not Scrypt().verify("wrong", stored)


# -- §7.5: a failure inside the guard --------------------------------------------------------------
async def test_a_failing_identity_policy_answers_500_with_the_headers(serve, tmp_path,
                                                                      monkeypatch):
    """§7.5: the guard answers its own failure; a 500 carries the security headers, the
    `Cache-Control` of the same section, and the fixed body — never the failure's own words."""
    import sqlite3

    server = await start(serve, tmp_path)
    _, token = await login(server.running, "alice")

    def unreachable(_token):
        raise sqlite3.OperationalError("the database is gone")

    monkeypatch.setattr(server.store, "login_account", unreachable)
    response = await get(server.running, "/api/health", token)
    assert response.status_code == 500
    assert response.headers["content-security-policy"] == \
        "default-src 'self'; frame-ancestors 'none'"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-cache"
    assert response.text == "Internal Server Error"


async def test_a_handshake_whose_identity_check_fails_is_closed_with_1011(serve, tabs, tmp_path,
                                                                         monkeypatch):
    import sqlite3

    server = await start(serve, tmp_path)
    _, token = await login(server.running, "alice")

    def unreachable(_token):
        raise sqlite3.OperationalError("the database is gone")

    monkeypatch.setattr(server.store, "login_account", unreachable)
    tab = await tabs(server.running, origin=server.running.origin, headers=ws_headers(token))
    assert await tab.close_code() == 1011


# -- §4.3: the per-identity socket cap ----------------------------------------------------------------
async def test_a_socket_past_the_identity_cap_is_closed_and_a_closed_one_frees_its_place(
        serve, tabs, tmp_path):
    """§4.3, §7.6: one identity holds at most the 32 of the limits table — the app runs that cap,
    not a number the test lowered — and the 33rd is closed with 4429, the cap's own code, not the
    1013 of queue overflow, so the page can stop reconnecting (§3.8). Closing one socket gives
    the place back, so a tab that reconnects after a reload is not locked out by its own
    predecessor."""
    from gowui.spaces import MAX_TABS

    server = await start(serve, tmp_path)
    running = server.running
    _, token = await login(running, "alice")
    assert (running.app.state.registry.max_tabs, MAX_TABS) == (32, 32)

    async def open_one():
        tab = await tabs(running, origin=running.origin, headers=ws_headers(token))
        assert not isinstance(tab, int), f"the handshake was refused with HTTP {tab}"
        return tab

    held = []
    for _ in range(MAX_TABS):
        tab = await open_one()
        assert await tab.wait_state() is not None
        held.append(tab)
    extra = await open_one()
    assert await extra.close_code() == 4429
    assert all(tab.open for tab in held)

    await held[0].close()
    key = next(iter(running.app.state.registry.live))
    await wait_for(lambda: len(running.app.state.registry.live[key].hub.tabs) == MAX_TABS - 1,
                   HANG)
    replacement = await open_one()
    assert await replacement.wait_state() is not None, "a closed socket freed no place"
