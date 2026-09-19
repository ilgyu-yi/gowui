"""Shared helpers for the server-mode tests (SPEC §5, §6, §7.1–§7.11, §8.4, §9, §10).

A server-mode app is built from ``GOWUI_*`` variables through ``ServerConfig.from_env``, over a
temporary SQLite database, and served by the same uvicorn harness as the local tests
(tests/app_helpers.py). Password hashing uses scrypt with a tiny cost, injected into the store, so
no test depends on the real N = 2^14; a ``CountingHasher`` also counts verifications and can make
each one slow enough that concurrent sign-ins overlap.

The API these tests pin (the implementer builds to it)::

    from gowui.store import Store, Scrypt, UserExists
    Scrypt(n=2**14, r=8, p=1)                 # .hash(password) -> str; .verify(password, stored)
    Store(path, *, hasher=Scrypt())           # the §8.4 database; .close()
        add_user(name, password)              # raises UserExists
        set_password(name, password) -> bool  # False: no such name
        remove_user(name) -> bool
        list_users() -> list[str]             # sorted names
        check_password(name, password) -> Verified | None     # runs the hasher once
        open_login(verified, seconds) -> str | None           # None: the hash changed meanwhile
        login_account(token) -> tuple[str, str] | None        # (identity key, name)
        close_login(token)
        load_state(key) / save_state(key, snapshot) / set_aside(key, reason)

    from gowui.server_mode import ServerConfig, ConfigError, server_policies, SIGN_IN_TEXT
    ServerConfig.from_env(env: Mapping[str, str]) -> ServerConfig   # ConfigError(.variable)
    server_policies(config, store) -> Policies
    create_app(policies, *, startup=(), revalidate_interval=30.0)
"""

from __future__ import annotations

import dataclasses
import json
import re
import threading
import time
from pathlib import Path

LOOPBACK = "127.0.0.1"
PASSWORD = "correct horse"
COOKIE = "gowui_session"


class CountingHasher:
    """Low-cost scrypt that counts verifications; ``delay`` makes each one take that long."""

    def __init__(self, delay: float = 0.0) -> None:
        from gowui.store import Scrypt

        self.inner = Scrypt(n=2 ** 4, r=1, p=1)
        self.delay = delay
        self.verifications = 0
        self._lock = threading.Lock()

    def hash(self, password: str) -> str:
        return self.inner.hash(password)

    def verify(self, password: str, stored: str) -> bool:
        with self._lock:
            self.verifications += 1
        if self.delay:
            time.sleep(self.delay)
        return self.inner.verify(password, stored)


def open_store(path: Path, hasher=None):
    from gowui.store import Store

    return Store(path, hasher=hasher or CountingHasher())


def catalog(*entries: dict) -> str:
    return json.dumps(list(entries))


def config(db: Path, **env: str):
    """A ``ServerConfig`` from ``GOWUI_*`` variables (keys without the prefix, lower case)."""
    from gowui.server_mode import ServerConfig

    variables = {"GOWUI_DB": str(db)}
    variables.update({f"GOWUI_{k.upper()}": v for k, v in env.items()})
    return ServerConfig.from_env(variables)


def build_app(store, cfg, *, idle: float | None = None, revalidate: float = 30.0):
    from gowui.app import create_app
    from gowui.server_mode import server_policies

    policies = server_policies(cfg, store)
    if idle is not None:
        policies = dataclasses.replace(policies, idle_release_seconds=idle)
    return create_app(policies, revalidate_interval=revalidate)


@dataclasses.dataclass
class Server:
    running: object
    store: object
    hasher: CountingHasher
    db: Path


async def start(serve, tmp_path: Path, *, users=("alice", "bob"), hasher=None,
                idle: float | None = None, revalidate: float = 30.0, db: Path | None = None,
                **env: str) -> Server:
    """A running server-mode app with the given password accounts (password ``PASSWORD``)."""
    hasher = hasher or CountingHasher()
    db = db or tmp_path / "data" / "gowui.db"
    store = open_store(db, hasher)
    for name in users:
        store.add_user(name, PASSWORD)
    running = await serve(build_app(store, config(db, **env), idle=idle, revalidate=revalidate))
    return Server(running, store, hasher, db)


async def login(running, name: str, password: str = PASSWORD, *, headers: dict | None = None,
                client=None):
    """POST the sign-in form; ``(response, token or None)``."""
    own = client is None
    client = client or running.client()
    try:
        response = await client.post(
            "/login", data={"name": name, "password": password},
            headers={"Host": running.host, "Origin": running.origin, **(headers or {})},
            follow_redirects=False)
    finally:
        if own:
            await client.aclose()
    return response, response.cookies.get(COOKIE)


def cookie_header(token: str) -> dict:
    return {"Cookie": f"{COOKIE}={token}"}


def ws_headers(token: str) -> list[tuple[str, str]]:
    return [("Cookie", f"{COOKIE}={token}")]


async def get(running, path: str, token: str | None = None, **headers: str):
    async with running.client() as client:
        return await client.get(path, headers={"Host": running.host,
                                               **(cookie_header(token) if token else {}),
                                               **headers}, follow_redirects=False)


def location_error(response) -> str | None:
    found = re.search(r"[?&]error=(\w+)", response.headers.get("location", ""))
    return found.group(1) if found else None
