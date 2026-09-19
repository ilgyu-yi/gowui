"""Server mode: per-account isolation, persistence and idle release (SPEC §3.1, §7.8, §8.2, §8.4;
issue #9 AC1 and AC7).

Two password accounts on one running server: nothing of Alice's boards reaches Bob over HTTP or
WebSocket, and nothing Bob sends changes them. The database outlives the app: a new app on the
same file gives Alice her boards back, an idle space is released and reloaded intact, and a name
that is removed and added again starts with fresh boards.
"""

from __future__ import annotations

from helpers import HANG, wait_for
from server_helpers import PASSWORD, get, login, start, ws_headers
from test_app_local import play


async def signed_in_tab(tabs, running, name: str):
    _, token = await login(running, name)
    assert token, f"{name} could not sign in"
    tab = await tabs(running, origin=running.origin, headers=ws_headers(token))
    assert not isinstance(tab, int), f"handshake refused with HTTP {tab}"
    assert await tab.wait_state() is not None, "no state on attach"
    return tab, token


def board_names(frame: dict) -> list[str]:
    return [b["name"] for b in frame["boards"]]


# -- AC1: isolation over WebSocket and HTTP ------------------------------------------------------
async def test_bob_sees_nothing_of_alices_boards(serve, tabs, tmp_path):
    server = await start(serve, tmp_path)
    running = server.running
    alice, alice_token = await signed_in_tab(tabs, running, "alice")
    await play(alice, "C3", "G7")
    start_mark = alice.mark()
    await alice.send({"type": "board_rename", "id": alice.state()["activeBoard"],
                      "name": "alice-secret"})
    await alice.wait_state(lambda f: "alice-secret" in board_names(f), start_mark)

    bob, bob_token = await signed_in_tab(tabs, running, "bob")
    frame = bob.state()
    assert (frame["game"]["moveCount"], "alice-secret" in board_names(frame)) == (0, False)
    assert "alice-secret" not in "".join(bob.texts)

    sgf = await get(running, "/api/sgf", bob_token)
    assert sgf.status_code == 200
    assert ";B[" not in sgf.text and ";W[" not in sgf.text


async def test_bob_cannot_change_alices_boards(serve, tabs, tmp_path):
    server = await start(serve, tmp_path)
    running = server.running
    alice, alice_token = await signed_in_tab(tabs, running, "alice")
    await play(alice, "C3")
    alice_board = alice.state()["activeBoard"]
    before = alice.mark()

    bob, bob_token = await signed_in_tab(tabs, running, "bob")
    for message in ({"type": "board_rename", "id": alice_board, "name": "bob-was-here"},
                    {"type": "board_delete", "id": alice_board},
                    {"type": "play", "color": "white", "vertex": "D4"},
                    {"type": "new_game", "size": 9, "komi": None, "rules": "japanese",
                     "handicap": 0}):
        await bob.send(message)
    async with running.client() as client:
        posted = await client.post("/api/sgf", content="(;GM[1]SZ[9];B[aa];W[bb];B[cc])",
                                   headers={"Host": running.host, "Origin": running.origin,
                                            "Cookie": f"gowui_session={bob_token}"})
    assert posted.status_code == 200
    await bob.wait_state(lambda f: f["game"]["moveCount"] == 3)

    await alice.send({"type": "state"})
    frame = await alice.wait_state(start=before)
    assert (frame["game"]["moveCount"], "bob-was-here" in board_names(frame),
            frame["activeBoard"]) == (1, False, alice_board)
    alice_sgf = await get(running, "/api/sgf", alice_token)
    assert "bb" not in alice_sgf.text


async def test_sso_and_password_accounts_with_one_name_are_apart(serve, tabs, tmp_path):
    """§7.3: ``sso:alice`` is not the password account ``alice``."""
    server = await start(serve, tmp_path, auth="local,header", trusted_proxies="127.0.0.1/32")
    running = server.running
    alice, _ = await signed_in_tab(tabs, running, "alice")
    await play(alice, "C3")
    sso = await tabs(running, origin=running.origin, headers=[("X-authentik-username", "alice")])
    assert not isinstance(sso, int)
    frame = await sso.wait_state()
    assert frame["game"]["moveCount"] == 0
    keys = sorted(running.app.state.registry.live)
    assert len(keys) == 2 and "sso:alice" in keys and "local:alice" not in keys


# -- AC7: persistence, idle release, reuse of a name ----------------------------------------------
async def test_a_restarted_server_gives_alice_her_boards_back(serve, tabs, tmp_path):
    first = await start(serve, tmp_path)
    alice, token = await signed_in_tab(tabs, first.running, "alice")
    await play(alice, "C3", "G7")
    await alice.close()
    await first.running.stop()
    first.store.close()

    second = await start(serve, tmp_path, users=(), db=first.db)
    again, _ = await signed_in_tab(tabs, second.running, "alice")
    assert again.state()["game"]["moveCount"] == 2


async def test_an_idle_account_is_released_and_reloaded_intact(serve, tabs, tmp_path):
    server = await start(serve, tmp_path, idle=0.0)
    registry = server.running.app.state.registry
    alice, token = await signed_in_tab(tabs, server.running, "alice")
    await play(alice, "C3", "G7", "D5")
    key = next(iter(registry.live))
    await alice.close()
    await wait_for(lambda: not registry.live[key].hub.tabs, HANG)
    await registry.release_idle()
    assert key not in registry.live

    again = await tabs(server.running, origin=server.running.origin, headers=ws_headers(token))
    frame = await again.wait_state()
    assert frame["game"]["moveCount"] == 3


async def test_a_removed_then_re_added_name_starts_with_fresh_boards(serve, tabs, tmp_path):
    """§7.1, §7.8: the new account gets a new id, so the old space is never re-bound."""
    server = await start(serve, tmp_path)
    alice, _ = await signed_in_tab(tabs, server.running, "alice")
    await play(alice, "C3", "G7")
    await alice.close()
    assert server.store.remove_user("alice")
    server.store.add_user("alice", PASSWORD)
    again, _ = await signed_in_tab(tabs, server.running, "alice")
    assert again.state()["game"]["moveCount"] == 0


async def test_a_re_added_name_starts_fresh_after_a_release_too(serve, tabs, tmp_path):
    server = await start(serve, tmp_path, idle=0.0)
    registry = server.running.app.state.registry
    alice, _ = await signed_in_tab(tabs, server.running, "alice")
    await play(alice, "C3")
    await alice.close()
    await wait_for(lambda: all(not s.hub.tabs for s in registry.live.values()), HANG)
    await registry.release_idle()
    assert server.store.remove_user("alice")
    server.store.add_user("alice", PASSWORD)
    again, _ = await signed_in_tab(tabs, server.running, "alice")
    assert again.state()["game"]["moveCount"] == 0


def test_a_save_for_a_removed_account_writes_nothing(tmp_path):
    """§8.4: a ``local:`` key is saved only while that account exists."""
    from app_helpers import snapshot_with
    from server_helpers import open_store

    store = open_store(tmp_path / "gowui.db")
    store.add_user("alice", PASSWORD)
    verified = store.check_password("alice", PASSWORD)
    key, _ = store.login_account(store.open_login(verified, 60))
    assert store.remove_user("alice")
    store.save_state(key, snapshot_with("C3"))
    assert store.load_state(key) is None

