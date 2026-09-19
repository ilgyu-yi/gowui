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
