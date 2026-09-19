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
            login(running, "alice", f"wrong {i}", client=client) for i in range(20)))
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


async def test_cookie_secure_follows_a_trusted_forwarded_proto(serve, tmp_path):
    server = await start(serve, tmp_path, trusted_proxies="127.0.0.1/32")
    response, _ = await login(server.running, "alice", headers={"X-Forwarded-Proto": "https"})
    assert "secure" in response.headers["set-cookie"].lower()
