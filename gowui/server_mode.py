"""The policies of server mode (SPEC §6.1–§6.4, §7.1–§7.3, §7.9–§7.11, §8.4, §10): password
accounts and/or an SSO header believed only from trusted proxies, engines only from the catalog,
and one snapshot per account in SQLite.

Only the command line builds this bundle (§9); nothing below it knows which mode it runs in.
"""

from __future__ import annotations

import asyncio
import html
import ipaddress
import json
import math
import re
import threading
import time
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response

from .engine import PROTOCOLS
from .guard import client_address, request_is_https, trusted_peer
from .policies import Identity, Policies
from .session import EngineRequestError, EngineTarget
from .store import Store, valid_name, valid_password

__all__ = ["CatalogAddresses", "CatalogEntry", "ConfigError", "LoginThrottle", "SIGN_IN_TEXT",
           "ServerConfig", "ServerIdentity", "SqliteStorage", "server_policies"]

COOKIE = "gowui_session"
MAX_COOKIE = 128
LOGIN_BODY_CAP = 8 * 1024
CATALOG_REFUSAL = "choose an engine from the list"
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_ENGINE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")
_ENTRY_KEYS = {"id", "label", "protocol", "host", "port", "console"}
_REQUIRED_KEYS = {"id", "protocol", "host", "port"}


def _control(text: str) -> bool:
    return any(unicodedata.category(ch) == "Cc" for ch in text)


# -- configuration (§7.9, §10) -----------------------------------------------------------------------
class ConfigError(Exception):
    """A malformed ``GOWUI_*`` variable; ``variable`` names it."""

    def __init__(self, variable: str, message: str) -> None:
        super().__init__(f"{variable}: {message}")
        self.variable = variable


@dataclass(frozen=True)
class CatalogEntry:
    id: str
    label: str
    protocol: str
    host: str
    port: int
    console: bool = False


@dataclass(frozen=True)
class ServerConfig:
    db: Path
    auth: frozenset[str]
    auth_header: str
    trusted_proxies: tuple
    logout_url: str
    session_days: float
    cookie_secure: str
    engines: tuple[CatalogEntry, ...]
    idle_minutes: float

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ServerConfig":
        def get(name: str, default: str) -> str:
            value = env.get(name) or ""
            return value if value else default

        auth_text = get("GOWUI_AUTH", "local")
        if auth_text not in ("local", "header", "local,header"):
            raise ConfigError("GOWUI_AUTH", "must be local, header or local,header")
        auth = frozenset(auth_text.split(","))

        header = get("GOWUI_AUTH_HEADER", "X-authentik-username")
        if not _TOKEN.fullmatch(header):
            raise ConfigError("GOWUI_AUTH_HEADER", "must be an HTTP header name")

        proxies = []
        proxies_text = get("GOWUI_TRUSTED_PROXIES", "")
        if proxies_text:
            for part in proxies_text.split(","):
                try:
                    proxies.append(ipaddress.ip_network(part.strip(), strict=True))
                except ValueError:
                    raise ConfigError("GOWUI_TRUSTED_PROXIES",
                                      "must be comma-separated IP addresses or networks") from None
        if "header" in auth and not proxies:
            raise ConfigError("GOWUI_TRUSTED_PROXIES",
                              "is required when GOWUI_AUTH includes header")

        logout_url = get("GOWUI_LOGOUT_URL", "")
        if logout_url:
            parts = urlsplit(logout_url)
            if (parts.scheme not in ("http", "https") or not parts.netloc
                    or any(ch.isspace() for ch in logout_url) or _control(logout_url)):
                raise ConfigError("GOWUI_LOGOUT_URL", "must be an absolute http or https URL")

        def positive(name: str, default: str) -> float:
            try:
                value = float(get(name, default))
            except ValueError:
                raise ConfigError(name, "must be a number above 0") from None
            if not math.isfinite(value) or value <= 0:
                raise ConfigError(name, "must be a finite number above 0")
            return value

        days = positive("GOWUI_SESSION_DAYS", "14")
        idle = positive("GOWUI_IDLE_MINUTES", "10")

        secure = get("GOWUI_COOKIE_SECURE", "auto")
        if secure not in ("auto", "1", "0"):
            raise ConfigError("GOWUI_COOKIE_SECURE", "must be auto, 1 or 0")

        engines = _catalog(get("GOWUI_ENGINES", "[]"))
        return cls(db=Path(get("GOWUI_DB", "./data/gowui.db")), auth=auth, auth_header=header,
                   trusted_proxies=tuple(proxies), logout_url=logout_url, session_days=days,
                   cookie_secure=secure, engines=engines, idle_minutes=idle)


def _catalog(text: str) -> tuple[CatalogEntry, ...]:
    def bad(message: str) -> ConfigError:
        return ConfigError("GOWUI_ENGINES", message)

    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise bad("must be a JSON list") from None
    if not isinstance(value, list):
        raise bad("must be a JSON list")
    entries: list[CatalogEntry] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        where = f"entry {index + 1}"
        if not isinstance(item, dict):
            raise bad(f"{where} must be an object")
        keys = set(item)
        if keys - _ENTRY_KEYS or _REQUIRED_KEYS - keys:
            raise bad(f"{where} must have id, protocol, host, port and optionally label, console")
        ident = item["id"]
        if not isinstance(ident, str) or not _ENGINE_ID.fullmatch(ident) or ident in seen:
            raise bad(f"{where} has an invalid or repeated id")
        seen.add(ident)
        label = item.get("label", ident)
        if not isinstance(label, str) or not label or _control(label):
            raise bad(f"{where} has an invalid label")
        protocol = item["protocol"]
        if protocol not in PROTOCOLS:
            raise bad(f"{where} has an unknown protocol")
        host = item["host"]
        if (not isinstance(host, str) or not host
                or any(ch.isspace() for ch in host) or _control(host)):
            raise bad(f"{where} has an invalid host")
        port = item["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise bad(f"{where} has an invalid port")
        console = item.get("console", False)
        if not isinstance(console, bool):
            raise bad(f"{where} has an invalid console flag")
        entries.append(CatalogEntry(ident, label, protocol, host, port, console))
    return tuple(entries)


# -- engine addresses (§6.3, §7.7) ----------------------------------------------------------------------
class CatalogAddresses:
    """The browser sends ``{engineId}``; addresses never leave the server."""

    expose_address = False

    def __init__(self, entries: tuple[CatalogEntry, ...]) -> None:
        self.entries = {entry.id: entry for entry in entries}
        self.offers_console = any(entry.console for entry in entries)

    def resolve(self, request: Any) -> EngineTarget:
        if not isinstance(request, dict):
            raise EngineRequestError(CATALOG_REFUSAL)
        fields = {k: v for k, v in request.items() if k != "type"}
        ident = fields.get("engineId")
        if set(fields) != {"engineId"} or not isinstance(ident, str) or ident not in self.entries:
            raise EngineRequestError(CATALOG_REFUSAL)
        entry = self.entries[ident]
        return EngineTarget(protocol=entry.protocol, host=entry.host, port=entry.port,
                            request_echo={"engineId": ident}, console=entry.console)

    def describe(self) -> dict:
        return {"kind": "catalog", "engines": [
            {"id": e.id, "label": e.label, "protocol": e.protocol, "console": e.console}
            for e in self.entries.values()]}


# -- storage (§6.4, §8.4) ---------------------------------------------------------------------------------
class SqliteStorage:
    def __init__(self, store: Store) -> None:
        self.store = store

    def load(self, key: str) -> dict | None:
        return self.store.load_state(key)

    def save(self, key: str, snapshot: dict) -> None:
        self.store.save_state(key, snapshot)

    def set_aside(self, key: str, reason: str) -> None:
        self.store.set_aside(key, reason)


# -- login throttling (§7.1) --------------------------------------------------------------------------------
def throttle_client(address: str) -> str:
    """The throttle's client key: an IPv4 address (or IPv4-mapped IPv6) itself, other IPv6 by
    its /64 (§7.1). A string that is not an address is its own key."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


class LoginThrottle:
    """Attempts per (client, name), failures per client and failures per name in a sliding
    window (§7.1).

    ``reserve`` takes a slot before any password is checked, so concurrent attempts cannot all
    pass; a failure keeps its slot, a success clears the (client, name) entry and releases its
    own slot from the client's and the name's budgets. Clients are keyed by ``throttle_client``.
    """

    def __init__(self, *, clock=time.monotonic, window: float = 600.0, per_name: int = 5,
                 per_client: int = 30, per_account: int = 20,
                 max_entries: int = 10_000) -> None:
        self.clock = clock
        self.window = window
        self.per_name = per_name
        self.per_client = per_client
        self.per_account = per_account
        self.max_entries = max_entries
        self._names: dict[tuple[str, str], list[float]] = {}
        self._clients: dict[str, list[float]] = {}
        self._accounts: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _live(self, table: dict, key: Any, now: float) -> list[float]:
        stamps = [t for t in table.get(key, ()) if now - t < self.window]
        if stamps:
            table[key] = stamps
        else:
            table.pop(key, None)
        return stamps

    def _purge(self, now: float) -> None:
        for table in (self._names, self._clients, self._accounts):
            for key in list(table):
                self._live(table, key, now)

    def _room(self, table: dict, key: Any, now: float) -> bool:
        if key in table or len(table) < self.max_entries:
            return True
        self._purge(now)
        return key in table or len(table) < self.max_entries

    @staticmethod
    def _keys(client: str, name: str) -> tuple[str, str]:
        return throttle_client(client), name[:64]

    def reserve(self, client: str, name: str) -> float | None:
        """A slot for one attempt, or ``None`` when the attempt is throttled."""
        client, name = self._keys(client, name)
        with self._lock:
            now = self.clock()
            pair = (client, name)
            if (len(self._live(self._names, pair, now)) >= self.per_name
                    or len(self._live(self._clients, client, now)) >= self.per_client
                    or len(self._live(self._accounts, name, now)) >= self.per_account):
                return None
            if (not self._room(self._names, pair, now)
                    or not self._room(self._clients, client, now)
                    or not self._room(self._accounts, name, now)):
                return None  # a table is full of live entries: fail closed
            self._names.setdefault(pair, []).append(now)
            self._clients.setdefault(client, []).append(now)
            self._accounts.setdefault(name, []).append(now)
            return now

    def succeeded(self, client: str, name: str, slot: float) -> None:
        client, name = self._keys(client, name)
        with self._lock:
            self._names.pop((client, name), None)
            for table, key in ((self._clients, client), (self._accounts, name)):
                stamps = table.get(key)
                if stamps and slot in stamps:
                    stamps.remove(slot)
                    if not stamps:
                        del table[key]


# -- the sign-in page (§7.11) ---------------------------------------------------------------------------------
SIGN_IN_TEXT: dict[str, dict[str, str]] = {
    "en": {
        "title": "Sign in",
        "name": "Name",
        "password": "Password",
        "submit": "Sign in",
        "error.wrong": "Wrong name or password.",
        "error.throttled": "Too many attempts. Try again later.",
        "language": "Language",
        "lang.ko": "한국어",
        "lang.en": "English",
    },
    "ko": {
        "title": "로그인",
        "name": "이름",
        "password": "비밀번호",
        "submit": "로그인",
        "error.wrong": "이름 또는 비밀번호가 틀렸다.",
        "error.throttled": "시도가 너무 많다. 잠시 뒤 다시 시도한다.",
        "language": "언어",
        "lang.ko": "한국어",
        "lang.en": "English",
    },
}
ERRORS = ("wrong", "throttled")


def _page_language(request: Any) -> str:
    chosen = request.query_params.get("lang")
    if chosen in SIGN_IN_TEXT:
        return chosen
    first = request.headers.get("accept-language", "").split(",")[0].strip().lower()
    return "en" if first.startswith("en") else "ko"


def render_sign_in(lang: str, error: str | None) -> str:
    text = SIGN_IN_TEXT[lang]
    e = html.escape
    error_line = (f'<p class="error">{e(text["error." + error])}</p>\n'
                  if error in ERRORS else "")
    return f"""<!DOCTYPE html>
<html lang="{e(lang)}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(text["title"])} · gowui</title>
<link rel="stylesheet" href="/css/signin.css">
</head>
<body>
<main class="signin">
<h1>gowui</h1>
<form method="post" action="/login">
{error_line}<label>{e(text["name"])}
<input name="name" autocomplete="username" maxlength="64" required autofocus></label>
<label>{e(text["password"])}
<input type="password" name="password" autocomplete="current-password" maxlength="256" required></label>
<input type="hidden" name="lang" value="{e(lang)}">
<button type="submit">{e(text["submit"])}</button>
</form>
<nav class="languages" aria-label="{e(text["language"])}">
<a href="/login?lang=ko" lang="ko">{e(text["lang.ko"])}</a>
<a href="/login?lang=en" lang="en">{e(text["lang.en"])}</a>
</nav>
</main>
</body>
</html>
"""


# -- identity (§6.2, §7.1–§7.3) --------------------------------------------------------------------------
def _cookies(scope: dict, name: str) -> list[str]:
    found = []
    for key, value in scope.get("headers", []):
        if key.lower() != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            cookie_name, sep, cookie_value = part.strip().partition("=")
            if sep and cookie_name.strip() == name:
                found.append(cookie_value.strip())
    return found


class ServerIdentity:
    """SSO header from a trusted proxy first, then the session cookie (§6.2)."""

    def __init__(self, config: ServerConfig, store: Store, throttle: LoginThrottle | None = None,
                 *, max_verifying: int = 2, max_waiting: int = 8) -> None:
        self.config = config
        self.store = store
        self.throttle = throttle or LoginThrottle()
        self.max_waiting = max_waiting
        self._verifying = asyncio.Semaphore(max_verifying)
        self._waiting = 0
        self._header = config.auth_header.lower().encode("latin-1")

    # -- identify ---------------------------------------------------------------------------------
    def identify(self, conn: Any) -> Identity | None:
        scope = conn.scope
        config = self.config
        if "header" in config.auth and trusted_peer(scope, config.trusted_proxies):
            values = [v for k, v in scope.get("headers", []) if k.lower() == self._header]
            if len(values) == 1:
                try:
                    name = values[0].decode("utf-8")
                except UnicodeDecodeError:
                    name = None
                if name is not None and valid_name(name):
                    return Identity(key=f"sso:{name}", name=name, source="sso",
                                    logout_kind="sso" if config.logout_url else "",
                                    logout_url=config.logout_url)
        if "local" in config.auth:
            tokens = _cookies(scope, COOKIE)
            if len(tokens) == 1 and 0 < len(tokens[0]) <= MAX_COOKIE:
                found = self.store.login_account(tokens[0])
                if found is not None:
                    key, name = found
                    return Identity(key=key, name=name, source="local", logout_kind="local",
                                    logout_url="")
        return None

    # -- the sign-in routes ------------------------------------------------------------------------
    async def login_page(self, request: Any) -> Response:
        if "local" not in self.config.auth:
            return RedirectResponse("/", status_code=303)
        error = request.query_params.get("error")
        return HTMLResponse(render_sign_in(_page_language(request), error),
                            headers={"Cache-Control": "no-store"})

    async def login(self, request: Any) -> Response:
        if "local" not in self.config.auth:
            return RedirectResponse("/", status_code=303)
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > LOGIN_BODY_CAP:
            return PlainTextResponse("Request too large", status_code=413)
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > LOGIN_BODY_CAP:
                return PlainTextResponse("Request too large", status_code=413)
        try:
            fields = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True,
                              max_num_fields=4)
        except ValueError:
            fields = None
        lang = None
        if fields is not None:
            given = fields.get("lang", [None])[0]
            lang = given if given in SIGN_IN_TEXT else None

        def refused(kind: str) -> Response:
            suffix = f"&lang={lang}" if lang else ""
            return RedirectResponse(f"/login?error={kind}{suffix}", status_code=303)

        name = (fields or {}).get("name", [""])[0]
        password = (fields or {}).get("password", [None])[0]
        client = client_address(request.scope, self.config.trusted_proxies)
        if fields is None or not valid_name(name) or not valid_password(password):
            return refused("wrong")  # malformed: no slot, no scrypt (§7.1)
        if self._waiting >= self.max_waiting:
            return refused("throttled")
        slot = self.throttle.reserve(client, name)
        if slot is None:
            return refused("throttled")
        self._waiting += 1
        waiting = True
        try:
            async with self._verifying:
                self._waiting -= 1
                waiting = False
                verified = await asyncio.to_thread(self.store.check_password, name, password)
        finally:
            if waiting:
                self._waiting -= 1
        token = None
        if verified is not None:
            token = await asyncio.to_thread(self.store.open_login, verified,
                                            self.config.session_days * 86400)
        if token is None:
            return refused("wrong")
        self.throttle.succeeded(client, name, slot)
        response = RedirectResponse("/", status_code=303)
        secure = self.config.cookie_secure == "1" or (
            self.config.cookie_secure == "auto"
            and request_is_https(request.scope, self.config.trusted_proxies))
        response.set_cookie(COOKIE, token, max_age=int(self.config.session_days * 86400),
                            path="/", httponly=True, samesite="lax", secure=secure)
        return response

    async def logout(self, request: Any) -> Response:
        for token in _cookies(request.scope, COOKIE):
            if 0 < len(token) <= MAX_COOKIE:
                await asyncio.to_thread(self.store.close_login, token)
        target = "/login" if "local" in self.config.auth else "/"
        response = RedirectResponse(target, status_code=303)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="lax")
        return response


def server_policies(config: ServerConfig, store: Store, *,
                    throttle: LoginThrottle | None = None) -> Policies:
    """The server bundle (§6.1)."""
    return Policies(
        identity=ServerIdentity(config, store, throttle),
        engines=CatalogAddresses(config.engines),
        storage=SqliteStorage(store),
        idle_release_seconds=config.idle_minutes * 60,
        allowed_hosts=None,
        allow_ip_literals=True,
        trusted_proxies=config.trusted_proxies,
        sign_in_url="/login" if "local" in config.auth else None,
    )
