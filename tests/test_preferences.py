"""Per-account preferences: the UI language and the user's tuple presets (SPEC §3.8
"Preferences", §4.1 ``preferences``, §4.2, §6.4, §7.6, §8.4, §8.5; issue #23).

Where a preference lives is the storage policy's choice. Server mode keeps both per identity key
in SQLite, so the same account finds them in a second browser and after a restart; local mode
keeps none, `state.preferences` is `null`, and the page goes on using browser storage (§8.5).
The message that changes them is bounded like every other message (§7.6).

The API these tests pin (the implementer builds to it)::

    from gowui.session import GameSession, clean_preferences
    GameSession(resolve_engine, *, expose_address=True, broadcast=None, preferences=None)
        preferences=None            # the policy keeps none: state.preferences is null
        preferences={...}           # kept; {} starts empty
        session.preferences -> dict | None      # what a save would store

    storage policy (§6.4), next to load / save / set_aside:
        keeps_preferences: bool
        load_preferences(key) -> dict | None    # drops what the §4.1 rules refuse
        save_preferences(key, preferences) -> None

    from gowui.store import Store
        load_preferences(key) / save_preferences(key, preferences)
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from frontend_helpers import spec_section
from helpers import HANG, wait_for
from server_helpers import PASSWORD, login, open_store, start, ws_headers

SHARP = {"temperature": 0.5}
LAMBDA = {"lambda_utility": 0.5, "trust_mu": 1, "fill_kappa": 0}
EMPTY = {"lang": None, "presets": []}


def preset(name: str, tuple_: dict | None = None) -> dict:
    return {"name": name, "tuple": SHARP if tuple_ is None else tuple_}


# -- AC3: the SPEC says where each preference lives ------------------------------------------------
def test_the_spec_server_database_holds_a_preferences_table():
    """§8.4: one row per identity key, holding the language and the presets."""
    section = spec_section("### 8.4")
    rows = [line for line in section.splitlines() if line.startswith("| `preferences` |")]
    assert len(rows) == 1, section[:400]
    assert "identity key (primary key)" in rows[0], rows[0]
    assert '"lang"' in section and '"presets"' in section


def test_the_spec_browser_storage_is_the_fallback_not_the_home():
    """§8.5: the two `localStorage` keys hold them only while the policy keeps none."""
    section = spec_section("### 8.5")
    assert "`gowui.lang`" in section and "`gowui.userPresets`" in section
    assert "§8.4" in section and "§6.4" in section
    assert "`null`" in section


def test_the_spec_storage_policy_carries_the_preferences_capability():
    """§6.4: the capability and the two calls, and local mode keeps none."""
    section = spec_section("### 6.4")
    for name in ("`keeps_preferences: bool`", "`load_preferences(key)`",
                 "`save_preferences(key, preferences)`"):
        assert name in section, name


def test_the_spec_state_frame_carries_the_preferences():
    section = spec_section("### 4.2")
    assert "`preferences`" in section


def test_the_spec_bounds_the_preferences_message():
    """§7.6: a size cap, a preset count cap and the name caps."""
    section = spec_section("### 7.6")
    rows = {line.split("|")[1].strip(): line.split("|")[2].strip()
            for line in section.splitlines() if line.startswith("| ")}
    assert rows.get("`preferences` message") == "64 KiB as JSON"
    assert rows.get("Tuple presets per identity") == "64"
    assert rows.get("Preset name") == "40 characters"
    assert rows.get("UI language name (`lang`)") == "16 characters"


def test_the_spec_says_the_page_holds_the_same_bounds():
    """§3.8: the page refuses what the server would, since `preferences` is refused whole."""
    section = spec_section("### 3.8")
    assert "refused whole" in section
    assert "over 40 characters" in section and "65th preset" in section


def test_the_spec_says_a_refusal_puts_the_menu_back():
    """§3.8: after an `error` the page shows what the account holds, not a phantom entry."""
    section = spec_section("### 3.8")
    assert "back to what the last `state` carried" in section


# -- the storage policies' capability (§6.4) --------------------------------------------------------
def test_the_local_storages_keep_no_preferences(tmp_path):
    from gowui.local_mode import JsonFileStorage, MemoryStorage

    assert (MemoryStorage().keeps_preferences,
            JsonFileStorage(tmp_path / "state.json").keeps_preferences) == (False, False)


def test_the_sqlite_storage_keeps_preferences(tmp_path):
    from gowui.server_mode import SqliteStorage

    store = open_store(tmp_path / "gowui.db")
    try:
        storage = SqliteStorage(store)
        assert storage.keeps_preferences is True
        assert storage.load_preferences("sso:alice") is None
        storage.save_preferences("sso:alice", {"lang": "ko", "presets": [preset("mine")]})
        assert storage.load_preferences("sso:alice") == {"lang": "ko",
                                                         "presets": [preset("mine")]}
    finally:
        store.close()


def test_a_stored_preferences_value_is_read_with_what_it_refuses_dropped(tmp_path):
    """§6.4: a hand-edited row comes back cleaned, not refused, as a browser preset does (§8.5)."""
    from gowui.server_mode import SqliteStorage

    db = tmp_path / "gowui.db"
    store = open_store(db)
    try:
        storage = SqliteStorage(store)
        storage.save_preferences("sso:alice", EMPTY)
        _overwrite(db, "sso:alice", json.dumps({
            "lang": 7,
            "presets": [preset("keeps"), preset("out of range", {"min_p": 2}),
                        {"name": "  ", "tuple": {}}, "not an entry",
                        preset("a\nfaked dialog line"), preset("lambda", LAMBDA)]}))
        assert storage.load_preferences("sso:alice") == {
            "lang": None, "presets": [preset("keeps"), preset("lambda", LAMBDA)]}
    finally:
        store.close()


@pytest.mark.parametrize("text", ["not json at all", "[1, 2, 3]", '"a string"'])
def test_a_preferences_row_that_is_not_a_json_object_is_ignored(tmp_path, text):
    from gowui.server_mode import SqliteStorage

    db = tmp_path / "gowui.db"
    store = open_store(db)
    try:
        storage = SqliteStorage(store)
        storage.save_preferences("sso:alice", EMPTY)
        _overwrite(db, "sso:alice", text)
        assert storage.load_preferences("sso:alice") is None
    finally:
        store.close()


def _overwrite(db, key: str, text: str) -> None:
    """Put ``text`` in the account's preferences row, behind the store's back."""
    connection = sqlite3.connect(str(db), timeout=5.0, isolation_level=None)
    try:
        connection.execute("UPDATE preferences SET preferences = ? WHERE account = ?", (text, key))
    finally:
        connection.close()


# -- the database (§8.4) ---------------------------------------------------------------------------
def test_preferences_are_keyed_by_the_identity_key(tmp_path):
    store = open_store(tmp_path / "gowui.db")
    try:
        store.save_preferences("sso:alice", {"lang": "ko", "presets": []})
        store.save_preferences("sso:bob", {"lang": "en", "presets": [preset("bob")]})
        assert store.load_preferences("sso:alice") == {"lang": "ko", "presets": []}
        assert store.load_preferences("sso:bob") == {"lang": "en", "presets": [preset("bob")]}
        assert store.load_preferences("sso:carol") is None
    finally:
        store.close()


def test_removing_an_account_removes_its_preferences(tmp_path):
    store = open_store(tmp_path / "gowui.db")
    try:
        store.add_user("alice", PASSWORD)
        verified = store.check_password("alice", PASSWORD)
        key, _ = store.login_account(store.open_login(verified, 60))
        store.save_preferences(key, {"lang": "ko", "presets": [preset("mine")]})
        assert store.load_preferences(key) is not None
        assert store.remove_user("alice")
        assert store.load_preferences(key) is None
    finally:
        store.close()


def test_a_preferences_save_for_a_removed_account_writes_nothing(tmp_path):
    """§8.4: a ``local:`` key is written only while that account exists."""
    store = open_store(tmp_path / "gowui.db")
    try:
        store.add_user("alice", PASSWORD)
        verified = store.check_password("alice", PASSWORD)
        key, _ = store.login_account(store.open_login(verified, 60))
        assert store.remove_user("alice")
        store.save_preferences(key, {"lang": "ko", "presets": []})
        assert store.load_preferences(key) is None
    finally:
        store.close()


# -- the session: the state field and the message (§4.1, §4.2) ---------------------------------------
async def test_a_session_whose_storage_keeps_none_reports_null(h):
    frame = await h.fresh_state()
    assert frame["preferences"] is None
    assert h.session.preferences is None


async def test_a_session_that_keeps_them_reports_them(make_session):
    harness = make_session(preferences={"lang": "ko", "presets": [preset("mine")]})
    frame = await harness.fresh_state()
    assert frame["preferences"] == {"lang": "ko", "presets": [preset("mine")]}


async def test_a_kept_but_empty_set_of_preferences_is_an_object(make_session):
    harness = make_session(preferences={})
    assert (await harness.fresh_state())["preferences"] == EMPTY


async def test_the_message_changes_the_language_and_the_presets(make_session):
    harness = make_session(preferences={})
    start = harness.rec.mark()
    await harness.send({"type": "preferences", "lang": "ko",
                        "presets": [preset("mine"), preset("other", LAMBDA)]})
    frame = await harness.rec.wait_state(lambda f: f["preferences"]["lang"] == "ko", start)
    assert frame is not None, harness.rec.errors(start)
    assert frame["preferences"]["presets"] == [preset("mine"), preset("other", LAMBDA)]
    assert harness.session.preferences == frame["preferences"]


async def test_an_absent_field_is_unchanged(make_session):
    harness = make_session(preferences={})
    await harness.send({"type": "preferences", "lang": "ko", "presets": [preset("mine")]})
    await harness.send({"type": "preferences", "presets": []})
    assert harness.session.preferences == {"lang": "ko", "presets": []}
    await harness.send({"type": "preferences"})
    assert harness.session.preferences == {"lang": "ko", "presets": []}


async def test_an_explicit_null_lang_clears_the_stored_language(make_session):
    """§4.1: `lang` is present, not absent, when it is `null` — the one way back to `null`."""
    harness = make_session(preferences={})
    await harness.send({"type": "preferences", "lang": "ko", "presets": [preset("mine")]})
    await harness.send({"type": "preferences", "lang": None})
    assert harness.session.preferences == {"lang": None, "presets": [preset("mine")]}
    assert (await harness.fresh_state())["preferences"]["lang"] is None


async def test_a_preset_name_may_hold_a_space(make_session):
    """§4.1: only whitespace *other* than a plain space is refused inside a name."""
    harness = make_session(preferences={})
    await harness.send({"type": "preferences", "presets": [preset("my sharp one")]})
    assert harness.session.preferences["presets"] == [preset("my sharp one")]


async def test_a_preset_name_is_stored_trimmed(make_session):
    harness = make_session(preferences={})
    await harness.send({"type": "preferences", "presets": [preset("  mine  ")]})
    assert harness.session.preferences["presets"] == [preset("mine")]


async def test_the_message_is_refused_when_the_policy_keeps_none(h):
    """§4.1: local mode keeps them in the browser, so there is nothing to store."""
    start = h.rec.mark()
    await h.send({"type": "preferences", "lang": "ko"})
    assert "preferences" in (await h.rec.wait_error(start) or "")
    assert (await h.fresh_state())["preferences"] is None


REFUSED = [
    ("lang is not a string", {"lang": 7}),
    ("lang is empty", {"lang": ""}),
    ("lang is too long", {"lang": "x" * 17}),
    ("lang has a space", {"lang": "e n"}),
    ("lang has a control character", {"lang": "e" + chr(0) + "n"}),
    ("lang has a lone surrogate", {"lang": "e\ud800n"}),
    ("presets is null", {"presets": None}),
    ("presets is not a list", {"presets": {"name": "mine", "tuple": {}}}),
    ("presets is a string", {"presets": "mine"}),
    ("too many presets", {"presets": [preset(f"n{i}") for i in range(65)]}),
    ("an entry is not an object", {"presets": ["mine"]}),
    ("an entry has no name", {"presets": [{"tuple": {}}]}),
    ("an entry has a blank name", {"presets": [preset("   ")]}),
    ("an entry name is too long", {"presets": [preset("x" * 41)]}),
    ("an entry name has a newline", {"presets": [preset("mine\nDelete everything?")]}),
    ("an entry name has a control character", {"presets": [preset("mi" + chr(7) + "ne")]}),
    ("an entry name has a lone surrogate", {"presets": [preset("mi\udc00ne")]}),
    ("an entry name is not a string", {"presets": [{"name": 7, "tuple": {}}]}),
    ("an entry has an extra key", {"presets": [{"name": "mine", "tuple": {}, "extra": 1}]}),
    ("an entry has no tuple", {"presets": [{"name": "mine"}]}),
    ("a tuple is out of range", {"presets": [preset("mine", {"min_p": 2})]}),
    ("a tuple has an unknown key", {"presets": [preset("mine", {"nonsense": 1})]}),
    ("a tuple is not an object", {"presets": [preset("mine", "warm")]}),
    ("the message is oversized", {"lang": "en", "presets": [], "filler": "x" * 70_000}),
]


@pytest.mark.parametrize("why,fields", REFUSED, ids=[case[0] for case in REFUSED])
async def test_a_malformed_or_oversized_message_is_refused_and_changes_nothing(
        make_session, why, fields):
    harness = make_session(preferences={})
    await harness.send({"type": "preferences", "lang": "ko", "presets": [preset("kept")]})
    before = harness.session.preferences
    start = harness.rec.mark()
    await harness.send({"type": "preferences", **fields})
    assert await harness.rec.wait_error(start) is not None, f"{why} was accepted"
    assert harness.session.preferences == before, why


async def test_a_preset_a_search_would_take_is_kept(make_session):
    """§4.1: tuples are checked as if searching (max visits 2), as the page's Import is, so a
    λ preset is not refused over the session's Visits setting."""
    harness = make_session(preferences={})
    await harness.send({"type": "engine_params", "maxVisits": 1})
    await harness.send({"type": "preferences", "presets": [preset("lambda", LAMBDA)]})
    assert harness.session.preferences["presets"] == [preset("lambda", LAMBDA)]


# -- AC1: the same account, a second browser and a restart (§8.4) -------------------------------------
async def signed_in_tab(tabs, running, name: str = "alice"):
    _, token = await login(running, name)
    assert token, f"{name} could not sign in"
    tab = await tabs(running, origin=running.origin, headers=ws_headers(token))
    assert not isinstance(tab, int), f"handshake refused with HTTP {tab}"
    assert await tab.wait_state() is not None, "no state on attach"
    return tab, token


CHOSEN = {"lang": "ko", "presets": [preset("mine"), preset("lambda", LAMBDA)]}


async def test_a_second_browser_of_the_same_account_sees_them(serve, tabs, tmp_path):
    """AC1: two sign-ins of one account — two browsers — share the language and the presets."""
    server = await start(serve, tmp_path)
    first, _ = await signed_in_tab(tabs, server.running)
    mark = first.mark()
    await first.send({"type": "preferences", **CHOSEN})
    assert await first.wait_state(lambda f: f["preferences"] == CHOSEN, mark) is not None

    second, _ = await signed_in_tab(tabs, server.running)
    assert second.state()["preferences"] == CHOSEN


async def test_an_open_second_browser_is_told_at_once(serve, tabs, tmp_path):
    server = await start(serve, tmp_path)
    first, _ = await signed_in_tab(tabs, server.running)
    second, _ = await signed_in_tab(tabs, server.running)
    mark = second.mark()
    await first.send({"type": "preferences", **CHOSEN})
    assert await second.wait_state(lambda f: f["preferences"] == CHOSEN, mark) is not None


async def test_another_account_keeps_its_own(serve, tabs, tmp_path):
    server = await start(serve, tmp_path)
    alice, _ = await signed_in_tab(tabs, server.running, "alice")
    mark = alice.mark()
    await alice.send({"type": "preferences", **CHOSEN})
    assert await alice.wait_state(lambda f: f["preferences"] == CHOSEN, mark) is not None
    bob, _ = await signed_in_tab(tabs, server.running, "bob")
    assert bob.state()["preferences"] == EMPTY


async def test_the_preferences_survive_a_restart(serve, tabs, tmp_path):
    first = await start(serve, tmp_path)
    alice, _ = await signed_in_tab(tabs, first.running)
    mark = alice.mark()
    await alice.send({"type": "preferences", **CHOSEN})
    assert await alice.wait_state(lambda f: f["preferences"] == CHOSEN, mark) is not None
    await alice.close()
    await first.running.stop()
    first.store.close()

    second = await start(serve, tmp_path, users=(), db=first.db)
    again, _ = await signed_in_tab(tabs, second.running)
    assert again.state()["preferences"] == CHOSEN


async def test_a_released_space_comes_back_with_them(serve, tabs, tmp_path):
    """§8.2: the preferences are saved on the passes that save the snapshot."""
    server = await start(serve, tmp_path, idle=0.0)
    registry = server.running.app.state.registry
    alice, token = await signed_in_tab(tabs, server.running)
    mark = alice.mark()
    await alice.send({"type": "preferences", **CHOSEN})
    assert await alice.wait_state(lambda f: f["preferences"] == CHOSEN, mark) is not None
    key = next(iter(registry.live))
    await alice.close()
    await wait_for(lambda: not registry.live[key].hub.tabs, HANG)
    await registry.release_idle()
    assert key not in registry.live

    again = await tabs(server.running, origin=server.running.origin, headers=ws_headers(token))
    frame = await again.wait_state()
    assert frame["preferences"] == CHOSEN


# -- AC2: local mode keeps using browser storage (§8.5) ----------------------------------------------
async def test_local_mode_reports_no_preferences_and_refuses_the_message(local_app, tabs):
    tab = await tabs(local_app)
    frame = await tab.wait_state()
    assert frame["preferences"] is None
    mark = tab.mark()
    await tab.send({"type": "preferences", "lang": "ko"})
    assert await tab.wait("error", start=mark) is not None
    fresh = tab.mark()
    await tab.send({"type": "state"})
    assert (await tab.wait_state(start=fresh))["preferences"] is None


async def test_a_local_state_file_holds_no_preferences(serve, tabs, tmp_path):
    """§6.4: preferences are never part of the snapshot, so the state file carries none."""
    from app_helpers import local_bundle

    from gowui.app import create_app
    from gowui.local_mode import JsonFileStorage

    path = tmp_path / "state.json"
    running = await serve(create_app(local_bundle(JsonFileStorage(path))))
    tab = await tabs(running)
    await tab.wait_state()
    await tab.send({"type": "play", "color": "black", "vertex": "D4"})
    await tab.wait_state(lambda f: f["game"]["moveCount"] == 1)
    await tab.close()
    await running.app.state.registry.save_changed()
    assert "preferences" not in json.loads(path.read_text())
