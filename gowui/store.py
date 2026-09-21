"""The server database (SPEC §7.1, §7.2, §8.4): password accounts, sign-in tokens and one snapshot
per identity key, in one SQLite file.

A password account has a random, immutable account id; its identity key is ``local:<id>``, so a
name that is removed and added again starts clean. Passwords are scrypt hashes; only the SHA-256
of a sign-in token is stored. A token is stored only if the hash that was verified is still the
account's current hash, in the same statement, so a sign-in racing ``passwd`` or ``remove`` gets
no token. Every method is synchronous and short; the server calls the slow ones in a worker thread.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Scrypt", "Store", "UserExists", "Verified", "valid_name", "valid_password"]

log = logging.getLogger("gowui")

MAX_NAME = 64
MIN_PASSWORD, MAX_PASSWORD = 8, 256
#: The byte cap of a stored snapshot row, as for the local state file (§8.3, §8.4).
SNAPSHOT_CAP = 64 * 1024 * 1024 * 6 + 1024 * 1024
#: The byte cap of a stored preferences row (§8.4), above the 64 KiB of the message that writes
#: it (§7.6) with room for the ASCII escaping of what it holds.
PREFERENCES_CAP = 128 * 1024

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (
    token_hash TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS states (
    account TEXT PRIMARY KEY,
    snapshot TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS preferences (
    account TEXT PRIMARY KEY,
    preferences TEXT NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS set_aside (
    account TEXT NOT NULL,
    snapshot TEXT NOT NULL,
    reason TEXT NOT NULL,
    at REAL NOT NULL
);
"""


class UserExists(Exception):
    """``add_user`` for a name that already exists."""


def valid_name(name: Any) -> bool:
    """1–64 printable characters without surrounding spaces (§7.1)."""
    return (isinstance(name, str) and 0 < len(name) <= MAX_NAME and name == name.strip()
            and name.isprintable())


def valid_password(password: Any) -> bool:
    return isinstance(password, str) and MIN_PASSWORD <= len(password) <= MAX_PASSWORD


#: The most hashing memory one call may ask for (§7.1). A setting above it is refused when a hash
#: is written, and a stored hash that asks for it is an error, never a quiet "wrong password".
MAX_HASH_MEMORY = 1024 * 1024 * 1024
#: The digest length of a stored hash, and of the dummy: ``hashlib.scrypt``'s default ``dklen``.
DIGEST_LENGTH = 64


def _maxmem(n: int, r: int, p: int) -> int:
    """The memory cap for these parameters: what scrypt needs, plus a megabyte of slack (§7.1).

    Derived, not fixed, so a hash written under a costlier setting still verifies instead of
    failing closed — a fixed cap would lock every account out the day the cost is raised.
    """
    return 128 * r * (n + p + 2) + 1024 * 1024


class Scrypt:
    """``scrypt$N$r$p$<salt>$<hash>`` hashes (§7.1).

    The defaults are OWASP's minimum written the cheaper way on memory: N = 2^16, r = 8, p = 2
    costs the same work as N = 2^17, r = 8, p = 1 at 64 MiB rather than 128 MiB. A stored hash
    carries the parameters it was made with, so hashes from an older, cheaper setting verify
    unchanged, each call capping scrypt's memory at what its own parameters need.
    """

    def __init__(self, n: int = 2 ** 16, r: int = 8, p: int = 2) -> None:
        self.n, self.r, self.p = n, r, p

    def hash(self, password: str) -> str:
        budget = _maxmem(self.n, self.r, self.p)
        if budget > MAX_HASH_MEMORY:
            raise ValueError(f"scrypt N={self.n}, r={self.r}, p={self.p} would need {budget} "
                             f"bytes, above the {MAX_HASH_MEMORY} this build allows")
        salt = os.urandom(16)
        digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=self.n, r=self.r,
                                p=self.p, maxmem=budget)
        b64 = base64.b64encode
        return f"scrypt${self.n}${self.r}${self.p}${b64(salt).decode()}${b64(digest).decode()}"

    def dummy(self) -> str:
        """A hash in stored form that no password matches, made without running scrypt (§7.1).

        Verifying against it costs exactly the one scrypt a real check costs, so a missing name
        takes what a wrong password takes, while opening the database costs no hash at all
        (§8.4): a ``gowui user list`` pays nothing for a hash it never uses.
        """
        b64 = base64.b64encode
        return (f"scrypt${self.n}${self.r}${self.p}${b64(os.urandom(16)).decode()}"
                f"${b64(os.urandom(DIGEST_LENGTH)).decode()}")

    def verify(self, password: str, stored: str) -> bool:
        try:
            scheme, n, r, p, salt, digest = stored.split("$")
            if scheme != "scrypt":
                return False
            n, r, p = int(n), int(r), int(p)
            expected = base64.b64decode(digest)
            salt_bytes = base64.b64decode(salt)
        except (ValueError, TypeError):
            return False
        budget = _maxmem(n, r, p)
        try:
            if budget > MAX_HASH_MEMORY:
                raise ValueError(f"it needs {budget} bytes, above {MAX_HASH_MEMORY}")
            actual = hashlib.scrypt(password.encode("utf-8"), salt=salt_bytes, n=n, r=r, p=p,
                                    dklen=len(expected), maxmem=budget)
        except (ValueError, MemoryError) as exc:
            # Loudly: this account cannot sign in until its password is set again (§7.1).
            log.error("gowui: a stored password hash asks for scrypt parameters this build will "
                      "not run (N=%s, r=%s, p=%s): %s", n, r, p, exc)
            return False
        return hmac.compare_digest(actual, expected)


@dataclass(frozen=True)
class Verified:
    """A name whose password was just verified, with the hash that verified it."""

    account_id: str
    name: str
    password_hash: str


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _local_key(account_id: str) -> str:
    return f"local:{account_id}"


class Store:
    """One SQLite connection behind a lock; safe to call from worker threads."""

    def __init__(self, path: str | os.PathLike, *, hasher: Any = None) -> None:
        self.path = Path(path)
        self.hasher = hasher if hasher is not None else Scrypt()
        # The dummy a missing name is checked against (§7.1). A hasher that can make one without
        # hashing does, so opening costs no scrypt; any other is asked for a hash of a secret.
        make_dummy = getattr(self.hasher, "dummy", None)
        self._dummy = (make_dummy() if make_dummy is not None
                       else self.hasher.hash(secrets.token_hex(16)))
        _prepare(self.path)
        self._db = sqlite3.connect(str(self.path), check_same_thread=False,
                                   isolation_level=None, timeout=5.0)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA busy_timeout = 5000")
            self._db.execute("PRAGMA journal_mode = WAL")
            _private_sidecars(self.path)
            self._db.executescript(SCHEMA)
            self._db.execute("DELETE FROM logins WHERE expires < ?", (time.time(),))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _query(self, sql: str, args: tuple = ()) -> list[tuple]:
        with self._lock:
            return self._db.execute(sql, args).fetchall()

    def _exec(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            return self._db.execute(sql, args).rowcount

    # -- accounts (§7.1, §8.4, §9) ------------------------------------------------------------------
    def add_user(self, name: str, password: str) -> None:
        hashed = self.hasher.hash(password)
        try:
            self._exec("INSERT INTO users (id, name, password, created) VALUES (?, ?, ?, ?)",
                       (secrets.token_hex(16), name, hashed, time.time()))
        except sqlite3.IntegrityError:
            raise UserExists(name) from None

    def set_password(self, name: str, password: str) -> bool:
        """Replace the account's hash and end its sessions; ``False`` when there is no such
        account. The lookup runs inside the transaction and the update must touch one row, so a
        ``remove`` that lands first is answered ``False``, never ``ok`` over nothing (§8.4)."""
        hashed = self.hasher.hash(password)
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
                changed = 0
                if row is not None:
                    changed = self._db.execute("UPDATE users SET password = ? WHERE id = ?",
                                               (hashed, row[0])).rowcount
                    if changed == 1:
                        self._db.execute("DELETE FROM logins WHERE account = ?",
                                         (_local_key(row[0]),))
                self._db.execute("COMMIT" if changed == 1 else "ROLLBACK")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return changed == 1

    def remove_user(self, name: str) -> bool:
        """Delete the account, its logins, its snapshot, its preferences and its set-aside rows;
        ``False`` when there is no such account. Like ``set_password`` (§8.4), the lookup runs
        inside the transaction and the delete must touch one row: a ``remove`` plus an ``add`` of
        the same name that lands first is answered ``False``, never ``ok`` over an account of that
        name that is still there."""
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
                removed = 0
                if row is not None:
                    key = _local_key(row[0])
                    removed = self._db.execute("DELETE FROM users WHERE id = ?",
                                               (row[0],)).rowcount
                    if removed == 1:
                        self._db.execute("DELETE FROM logins WHERE account = ?", (key,))
                        self._db.execute("DELETE FROM states WHERE account = ?", (key,))
                        self._db.execute("DELETE FROM preferences WHERE account = ?", (key,))
                        self._db.execute("DELETE FROM set_aside WHERE account = ?", (key,))
                self._db.execute("COMMIT" if removed == 1 else "ROLLBACK")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        return removed == 1

    def list_users(self) -> list[str]:
        return [row[0] for row in self._query("SELECT name FROM users ORDER BY name")]

    def check_password(self, name: str, password: str) -> Verified | None:
        """Run the hasher once, a missing name against a dummy hash (§7.1)."""
        rows = self._query("SELECT id, password FROM users WHERE name = ?", (name,))
        row = rows[0] if rows else None
        if row is None:
            self.hasher.verify(password, self._dummy)
            return None
        if not self.hasher.verify(password, row[1]):
            return None
        return Verified(row[0], name, row[1])

    # -- sign-in tokens (§7.2) ------------------------------------------------------------------------
    def open_login(self, verified: Verified, seconds: float) -> str | None:
        """A new token, stored only while the verified hash is still the account's hash."""
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            self._db.execute("DELETE FROM logins WHERE expires < ?", (now,))
            cursor = self._db.execute(
                "INSERT INTO logins (token_hash, account, expires) "
                "SELECT ?, ?, ? WHERE EXISTS "
                "(SELECT 1 FROM users WHERE id = ? AND name = ? AND password = ?)",
                (_token_hash(token), _local_key(verified.account_id), now + seconds,
                 verified.account_id, verified.name, verified.password_hash))
            stored = cursor.rowcount == 1
        return token if stored else None

    def login_account(self, token: str) -> tuple[str, str] | None:
        """``(identity key, name)`` of an unexpired login of an existing account.

        A read only: an expired row gives no identity here and is purged when the database is
        opened or a sign-in stores a token (§8.4), so the identity check on the event loop
        (§6.2) never writes.
        """
        if not token:
            return None
        now = time.time()
        with self._lock:
            row = self._db.execute(
                "SELECT logins.account, users.name FROM logins JOIN users "
                "ON logins.account = 'local:' || users.id "
                "WHERE logins.token_hash = ? AND logins.expires >= ?",
                (_token_hash(token), now)).fetchone()
        return (row[0], row[1]) if row else None

    def purge_logins(self) -> int:
        """Delete every expired login row; how many went (§7.2, §8.4).

        Opening the database and storing a token do this too; this is the same delete on its own,
        for the periodic pass (§8.2), so a row left by a browser that never comes back does not
        linger until one of those happens.
        """
        return self._exec("DELETE FROM logins WHERE expires < ?", (time.time(),))

    def close_login(self, token: str) -> None:
        if token:
            self._exec("DELETE FROM logins WHERE token_hash = ?", (_token_hash(token),))

    # -- snapshots (§6.4, §8.4) -------------------------------------------------------------------------
    def load_state(self, key: str) -> dict | None:
        rows = self._query("SELECT snapshot, length(CAST(snapshot AS BLOB)) FROM states "
                           "WHERE account = ?", (key,))
        if not rows:
            return None
        text, size = rows[0]
        if size is not None and size > SNAPSHOT_CAP:
            self._move_aside(key, f"is larger than {SNAPSHOT_CAP} bytes")
            return None
        try:
            value = json.loads(text)
        except (ValueError, RecursionError, TypeError):
            self._move_aside(key, "is not valid JSON")
            return None
        if not isinstance(value, dict):
            self._move_aside(key, "is not a JSON object")
            return None
        return value

    def save_state(self, key: str, snapshot: dict) -> None:
        # ASCII: SQLite stores text as UTF-8, which cannot hold a lone surrogate (§8.4).
        text = json.dumps(snapshot, ensure_ascii=True)
        now = time.time()
        upsert = ("ON CONFLICT(account) DO UPDATE SET snapshot = excluded.snapshot, "
                  "updated = excluded.updated")
        if key.startswith("local:"):
            # Written only while that account exists, in one statement (§8.4).
            self._exec("INSERT INTO states (account, snapshot, updated) SELECT ?, ?, ? "
                      "WHERE EXISTS (SELECT 1 FROM users WHERE 'local:' || id = ?) " + upsert,
                      (key, text, now, key))
        else:
            self._exec("INSERT INTO states (account, snapshot, updated) VALUES (?, ?, ?) "
                      + upsert, (key, text, now))

    def set_aside(self, key: str, reason: str) -> None:
        self._move_aside(key, reason)

    # -- preferences (§6.4, §8.4) -----------------------------------------------------------------------
    def load_preferences(self, key: str) -> dict | None:
        """The identity's stored preferences, or ``None`` when there are none.

        A row that is over the cap, is not valid JSON or is not a JSON object is ignored with a
        warning and the identity starts with none; it is never set aside, as it holds no game
        (§8.4). The policy takes the value from here through the rules of §4.1 (§6.4).
        """
        rows = self._query("SELECT preferences, length(CAST(preferences AS BLOB)) "
                           "FROM preferences WHERE account = ?", (key,))
        if not rows:
            return None
        text, size = rows[0]
        if size is not None and size > PREFERENCES_CAP:
            return self._bad_preferences(f"are larger than {PREFERENCES_CAP} bytes")
        try:
            value = json.loads(text)
        except (ValueError, RecursionError, TypeError):
            return self._bad_preferences("are not valid JSON")
        if not isinstance(value, dict):
            return self._bad_preferences("are not a JSON object")
        return value

    @staticmethod
    def _bad_preferences(reason: str) -> None:
        log.warning("gowui: the stored preferences of an account %s; the account starts with "
                    "none and the next change replaces them", reason)
        return None

    def save_preferences(self, key: str, preferences: dict) -> None:
        # ASCII, as the snapshot is: SQLite text cannot hold a lone surrogate (§8.4).
        text = json.dumps(preferences, ensure_ascii=True)
        now = time.time()
        upsert = ("ON CONFLICT(account) DO UPDATE SET preferences = excluded.preferences, "
                  "updated = excluded.updated")
        if key.startswith("local:"):
            # Written only while that account exists, in one statement (§8.4).
            self._exec("INSERT INTO preferences (account, preferences, updated) SELECT ?, ?, ? "
                       "WHERE EXISTS (SELECT 1 FROM users WHERE 'local:' || id = ?) " + upsert,
                       (key, text, now, key))
        else:
            self._exec("INSERT INTO preferences (account, preferences, updated) VALUES (?, ?, ?) "
                       + upsert, (key, text, now))

    def _move_aside(self, key: str, reason: str) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute("INSERT INTO set_aside (account, snapshot, reason, at) "
                                 "SELECT account, snapshot, ?, ? FROM states WHERE account = ?",
                                 (reason, time.time(), key))
                self._db.execute("DELETE FROM states WHERE account = ?", (key,))
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
        log.warning("gowui: the stored boards of an account %s; they were set aside and the "
                    "account starts fresh", reason)


def _prepare(path: Path) -> None:
    """Create the parent directory (0700) and the file (0600) before SQLite opens it (§8.4)."""
    directory = path.parent
    if not directory.exists():
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if os.name != "nt":
            os.chmod(directory, 0o700)
    if not path.exists():
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        os.close(fd)


def _private_sidecars(path: Path) -> None:
    """Set the ``-wal`` and ``-shm`` files to 0600 once WAL is on (§8.4)."""
    if os.name == "nt":
        return
    for suffix in ("-wal", "-shm"):
        try:
            os.chmod(f"{path}{suffix}", 0o600)
        except FileNotFoundError:
            pass
