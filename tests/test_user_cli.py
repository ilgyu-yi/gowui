"""``gowui user add|passwd|remove|list`` against a temporary database (SPEC §9, §8.4; issue #9 AC6).

The commands read only ``GOWUI_DB``: a malformed ``GOWUI_ENGINES`` or ``GOWUI_AUTH`` does not
block them. Passwords come from ``--password-stdin`` here (the prompt path is the same check).
"""

from __future__ import annotations

import io
import sqlite3

import pytest


@pytest.fixture
def user(tmp_path, monkeypatch, capsys):
    """``user(*argv, stdin="") -> (status, stdout, stderr)``"""
    from gowui.cli import main

    db = tmp_path / "data" / "gowui.db"
    monkeypatch.setenv("GOWUI_DB", str(db))
    monkeypatch.setenv("GOWUI_ENGINES", "not json")
    monkeypatch.setenv("GOWUI_AUTH", "nonsense")

    def run(*argv: str, stdin: str = ""):
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
        try:
            status = main(["user", *argv])
        except SystemExit as exited:
            status = exited.code
        out = capsys.readouterr()
        return status, out.out, out.err

    run.db = db
    return run


def test_add_then_list(user):
    assert user("add", "alice", "--password-stdin", stdin="password one\n")[:2] == (0, "ok\n")
    assert user("add", "bob", "--password-stdin", stdin="password two\r\n")[0] == 0
    assert user("list") == (0, "alice\nbob\n", "")


def test_list_of_an_empty_database_prints_nothing(user):
    assert user("list")[:2] == (0, "")


def test_add_refuses_an_existing_name_and_a_short_password(user):
    user("add", "alice", "--password-stdin", stdin="password one\n")
    status, _, err = user("add", "alice", "--password-stdin", stdin="password two\n")
    assert status == 1 and err.strip()
    status, _, err = user("add", "carol", "--password-stdin", stdin="short\n")
    assert status == 1 and err.strip()
    assert user("list")[1] == "alice\n"


def test_passwd_changes_the_password_and_ends_sessions(user):
    from gowui.store import Store

    user("add", "alice", "--password-stdin", stdin="password one\n")
    store = Store(user.db)
    token = store.open_login(store.check_password("alice", "password one"), 60)
    assert store.login_account(token) is not None
    assert user("passwd", "alice", "--password-stdin", stdin="password two\n")[:2] == (0, "ok\n")
    assert store.login_account(token) is None
    assert store.check_password("alice", "password one") is None
    assert store.check_password("alice", "password two") is not None
    store.close()


def test_passwd_and_remove_refuse_a_missing_name(user):
    assert user("passwd", "ghost", "--password-stdin", stdin="password one\n")[0] == 1
    assert user("remove", "ghost")[0] == 1


def test_remove_deletes_the_account_and_its_boards(user):
    user("add", "alice", "--password-stdin", stdin="password one\n")
    user("add", "bob", "--password-stdin", stdin="password two\n")
    from app_helpers import snapshot_with
    from gowui.store import Store

    store = Store(user.db)
    key, _ = store.login_account(store.open_login(store.check_password("alice", "password one"),
                                                  60))
    store.save_state(key, snapshot_with("C3"))
    store.close()
    assert user("remove", "alice")[:2] == (0, "ok\n")
    assert user("list")[1] == "bob\n"
    with sqlite3.connect(user.db) as db:
        counts = [db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("users", "logins", "states")]
    assert counts == [1, 0, 0]


def test_the_account_id_is_never_printed(user):
    _, out, err = user("add", "alice", "--password-stdin", stdin="password one\n")
    with sqlite3.connect(user.db) as db:
        (account_id,) = db.execute("SELECT id FROM users").fetchone()
    assert account_id not in out + err + user("list")[1]


# -- opening the database (§9, §8.4) --------------------------------------------------------------
def test_list_does_not_create_the_database(user):
    """§9: a database that is not there counts as empty; listing leaves no file behind."""
    assert user("list")[:2] == (0, "")
    assert not user.db.exists()


def test_a_database_that_cannot_be_opened_prints_one_line(user, tmp_path, monkeypatch):
    """§9: one stderr line and status 1, never a traceback."""
    blocking = tmp_path / "blocking"
    blocking.write_text("this is a file, not a directory")
    monkeypatch.setenv("GOWUI_DB", str(blocking / "gowui.db"))
    status, out, err = user("add", "alice", "--password-stdin", stdin="password one\n")
    assert (status, out) == (1, "")
    assert len(err.strip().splitlines()) == 1 and err.startswith("gowui: ")


# -- the accounts, under a race (§8.4) -------------------------------------------------------------
class _Rows:
    """A cursor stand-in holding rows already read."""

    def __init__(self, rows: list) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _RemovesAfterLookup:
    """The store's connection, with a removal landing right after the account id is read.

    That is the window `set_password` has to close: reading the id outside its own transaction
    lets a `remove` win the race and leaves an update that touches nothing (§8.4).

    The double fires on the text of the lookup, so a reworded statement would leave it watching
    for something the store no longer says and the test would pass while testing nothing. Every
    test using it therefore asserts `fired` afterwards.
    """

    def __init__(self, db, name: str) -> None:
        self._db = db
        self._name = name
        self.fired = False

    def __getattr__(self, attribute):
        return getattr(self._db, attribute)

    def _intervene(self) -> None:
        """What lands in the window: the account is removed."""
        self._db.execute("DELETE FROM users WHERE name = ?", (self._name,))

    def execute(self, sql, args=()):
        cursor = self._db.execute(sql, args)
        if not self.fired and sql.strip().upper().startswith("SELECT ID FROM USERS"):
            self.fired = True
            rows = cursor.fetchall()
            self._intervene()
            return _Rows(rows)
        return cursor


class _ReplacesAfterLookup(_RemovesAfterLookup):
    """The same window, with the name removed *and added again* under a new account id.

    A plain removal cannot tell a sound `remove` from an unsound one: when the loser reports `ok`
    over the row it did not delete, the account is gone either way. Re-adding it makes the
    difference visible — an `ok` would then say the account is gone while one of that name is
    there (§8.4).
    """

    def _intervene(self) -> None:
        self._db.execute("DELETE FROM users WHERE name = ?", (self._name,))
        self._db.execute("INSERT INTO users (id, name, password, created) VALUES (?, ?, ?, ?)",
                         ("a different account id", self._name, "not a hash", 0.0))


def test_passwd_never_reports_success_when_a_remove_wins_the_race(user):
    """§8.4: a `passwd` whose account goes away reports no such account and changes nothing."""
    from server_helpers import CountingHasher
    from gowui.store import Store

    user("add", "alice", "--password-stdin", stdin="password one\n")
    store = Store(user.db, hasher=CountingHasher())
    try:
        store._db = _RemovesAfterLookup(store._db, "alice")
        assert store.set_password("alice", "password two") is False
        assert store._db.fired, "the double never fired: the id lookup it watches for was reworded"
        store._db = store._db._db
        assert store.check_password("alice", "password one") is not None
        assert store.check_password("alice", "password two") is None
    finally:
        store.close()


def test_remove_never_reports_success_when_the_name_is_added_again_in_the_race(user):
    """§8.4: `remove` looks the name up inside its own transaction and must delete exactly one
    row, so a `remove` + `add` that lands first is answered "there is no account" rather than
    `ok` over an account of that name that is still there."""
    from server_helpers import CountingHasher
    from gowui.store import Store

    user("add", "alice", "--password-stdin", stdin="password one\n")
    store = Store(user.db, hasher=CountingHasher())
    try:
        store._db = _ReplacesAfterLookup(store._db, "alice")
        assert store.remove_user("alice") is False
        assert store._db.fired, "the double never fired: the id lookup it watches for was reworded"
        store._db = store._db._db
        assert "alice" in store.list_users()
    finally:
        store.close()


# -- sign-in tokens (§7.2, §8.4) --------------------------------------------------------------------
def test_a_cookie_lookup_leaves_the_purge_to_the_sign_in(user):
    """§8.4: a lookup only reads; expired rows go when the database is opened or a token is made."""
    from server_helpers import CountingHasher
    from gowui.store import Store

    user("add", "alice", "--password-stdin", stdin="password one\n")
    store = Store(user.db, hasher=CountingHasher())
    try:
        verified = store.check_password("alice", "password one")
        stale = store.open_login(verified, -1.0)
        assert store.login_account(stale) is None
        with sqlite3.connect(user.db) as db:
            assert db.execute("SELECT count(*) FROM logins").fetchone()[0] == 1
        fresh = store.open_login(verified, 60.0)
        assert store.login_account(fresh) is not None
        with sqlite3.connect(user.db) as db:
            assert db.execute("SELECT count(*) FROM logins").fetchone()[0] == 1
    finally:
        store.close()
