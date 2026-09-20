"""The guard in front of every HTTP request and WebSocket handshake (SPEC §5, §7.4, §7.5).

Pure ASGI middleware. In order it refuses duplicate ``Host`` / ``Origin`` / ``X-Forwarded-Host``
headers, applies the Host rule to the effective host, applies the Origin rule to handshakes and to
every request that is not ``GET`` or ``HEAD``, and resolves the identity (required except on the
public routes). It reads only the policy bundle's data (§6.1) and adds the §7.5 headers to every
response. Refusal bodies are fixed text: they never echo a header or the path. A failure inside
the guard itself — an identity policy that raises — is answered here, ``500`` (WebSocket
``1011``) with those headers, because nothing outside it would add them (§7.5).
"""

from __future__ import annotations

import ipaddress
import json
import logging
import unicodedata
from typing import Any

from starlette.requests import HTTPConnection

from .policies import Policies

__all__ = ["Guard", "IDENTITY_KEY", "WS_INTERNAL", "client_address", "forwarded_last",
           "is_ip_literal", "normalise_host", "peer_ip", "request_is_https", "trusted_peer"]

log = logging.getLogger("gowui")

#: The scope key under which the guard hands the resolved identity (or ``None``) to the routes.
IDENTITY_KEY = "gowui.identity"

CSP = "default-src 'self'; frame-ancestors 'none'"
SECURITY_HEADERS = [(b"content-security-policy", CSP.encode("ascii")),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-cache")]
_SECURITY_NAMES = {name for name, _ in SECURITY_HEADERS}

#: Headers of which a request may carry at most one (§7.4 rule 1).
_SINGLE = (b"host", b"origin", b"x-forwarded-host")
DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443}
#: Reachable without an identity, matched exactly (§5); ``/css/`` is the one public prefix.
PUBLIC_PATHS = frozenset({"/healthz", "/login", "/logout"})
PUBLIC_PREFIX = "/css/"

WS_FORBIDDEN = 4403
WS_UNAUTHENTICATED = 4401
#: A failure inside the guard itself: the WebSocket half of the ``500`` (§7.5).
WS_INTERNAL = 1011


def _bad_char(ch: str) -> bool:
    return ch in "@/\\" or ch.isspace() or unicodedata.category(ch) == "Cc"


def _port(text: str) -> int | None:
    if not (text.isascii() and text.isdigit()):
        return None
    value = int(text)
    return value if 1 <= value <= 65535 else None


def normalise_host(value: str) -> tuple[str, int | None] | None:
    """``(host, port)`` from a ``Host``-style value, or ``None`` when it is refused (§7.4).

    The port is stripped (``None`` when absent), ``[IPv6]`` unwrapped and the host lowercased.
    """
    if not value or any(_bad_char(ch) for ch in value):
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        try:
            host = ipaddress.IPv6Address(value[1:end]).compressed
        except ValueError:
            return None
        rest = value[end + 1:]
        if not rest:
            return host.lower(), None
        if not rest.startswith(":"):
            return None
        port = _port(rest[1:])
        return None if port is None else (host.lower(), port)
    host, sep, port_text = value.partition(":")
    if ":" in port_text or not host:
        return None  # an unbracketed host containing ':'
    if not sep:
        return host.lower(), None
    port = _port(port_text)
    return None if port is None else (host.lower(), port)


def is_ip_literal(host: str) -> bool:
    """Whether ``host`` is an IP literal: only what ``ipaddress.ip_address`` accepts (§7.4)."""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An IP address, an IPv4-mapped IPv6 address converted to its IPv4 form (§7.10)."""
    try:
        address = ipaddress.ip_address(text.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def peer_ip(scope: dict) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The TCP peer's address, or ``None`` when it is not an IP address."""
    client = scope.get("client")
    if not client or not isinstance(client[0], str):
        return None
    return _ip(client[0])


def trusted_peer(scope: dict, proxies: tuple) -> bool:
    """Whether the TCP peer is inside ``trusted_proxies`` (§7.3, §7.10)."""
    if not proxies:
        return False
    peer = peer_ip(scope)
    return peer is not None and any(peer in network for network in proxies)


def forwarded_last(scope: dict, name: bytes) -> str | None:
    """The last element of a forwarded list header, repeated lines joined in order (§7.10)."""
    values = [v.decode("latin-1") for k, v in scope.get("headers", []) if k.lower() == name]
    if not values:
        return None
    return ",".join(values).split(",")[-1].strip()


def request_is_https(scope: dict, proxies: tuple) -> bool:
    """The request's own scheme, or ``X-Forwarded-Proto`` from a trusted proxy (§7.10)."""
    if scope.get("scheme") in ("https", "wss"):
        return True
    if not trusted_peer(scope, proxies):
        return False
    last = forwarded_last(scope, b"x-forwarded-proto")
    return last is not None and last.lower() == "https"


def client_address(scope: dict, proxies: tuple) -> str:
    """The peer, or the last ``X-Forwarded-For`` address when the peer is a trusted proxy
    (§7.1, §7.10)."""
    peer = peer_ip(scope)
    if trusted_peer(scope, proxies):
        last = forwarded_last(scope, b"x-forwarded-for")
        forwarded = _ip(last) if last else None
        if forwarded is not None:
            return str(forwarded)
    if peer is not None:
        return str(peer)
    client = scope.get("client")
    return str(client[0]) if client else ""


def is_public(scope: dict) -> bool:
    """Whether the path is reachable without an identity (§5)."""
    path = scope.get("path", "")
    if path in PUBLIC_PATHS:
        return True
    if not path.startswith(PUBLIC_PREFIX):
        return False
    raw = scope.get("raw_path") or path.encode("utf-8", "surrogateescape")
    if b"%2f" in raw.lower():
        return False
    return ".." not in path.split("/")


class _Refusal(Exception):
    def __init__(self, status: int, ws_code: int) -> None:
        super().__init__(status)
        self.status = status
        self.ws_code = ws_code


_FORBIDDEN = _Refusal(403, WS_FORBIDDEN)
_INTERNAL = _Refusal(500, WS_INTERNAL)


class Guard:
    """ASGI middleware applying the §7.4 rules in front of ``app``."""

    def __init__(self, app: Any, policies: Policies) -> None:
        self.app = app
        self.policies = policies

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message.get("type") in ("http.response.start", "websocket.http.response.start"):
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in _SECURITY_NAMES]
                message = {**message, "headers": headers + SECURITY_HEADERS}
            await send(message)

        try:
            scope[IDENTITY_KEY] = self._check(scope)
        except _Refusal as refusal:
            await self._refuse(scope, receive, send_with_headers, refusal)
            return
        except Exception:  # noqa: BLE001 - the guard answers its own failure (§7.5)
            # Outside the server-error handler: nothing else would add the §7.5 headers.
            log.exception("gowui: the guard failed")
            await self._refuse(scope, receive, send_with_headers, _INTERNAL)
            return
        await self.app(scope, receive, send_with_headers)

    # -- the rules ----------------------------------------------------------------------------
    def _check(self, scope: dict) -> Any:
        policies = self.policies
        seen: dict[bytes, list[str]] = {}
        for name, value in scope.get("headers", []):
            name = name.lower()
            if name in _SINGLE:
                seen.setdefault(name, []).append(value.decode("latin-1"))
        if any(len(seen.get(name, ())) > 1 for name in _SINGLE):
            raise _FORBIDDEN
        hosts = seen.get(b"host")
        if not hosts:
            raise _FORBIDDEN
        parsed = normalise_host(hosts[0])
        if parsed is None:
            raise _FORBIDDEN
        scheme = scope.get("scheme", "http")
        if trusted_peer(scope, policies.trusted_proxies):
            forwarded = seen.get(b"x-forwarded-host")
            if forwarded:
                parsed = normalise_host(forwarded[0])
                if parsed is None:
                    raise _FORBIDDEN
            if request_is_https(scope, policies.trusted_proxies):
                scheme = "https"
        host, port = parsed
        if port is None:
            port = DEFAULT_PORTS.get(scheme, 80)
        allowed = policies.allowed_hosts
        if allowed is not None and host not in allowed and not (
                policies.allow_ip_literals and is_ip_literal(host)):
            raise _FORBIDDEN
        if scope["type"] == "websocket" or scope.get("method") not in ("GET", "HEAD"):
            origins = seen.get(b"origin")
            if origins and not _origin_matches(origins[0], host, port):
                raise _FORBIDDEN
        identity = policies.identity.identify(HTTPConnection(scope))
        if identity is None and not is_public(scope):
            raise _Refusal(401, WS_UNAUTHENTICATED)
        return identity

    # -- refusals -----------------------------------------------------------------------------
    async def _refuse(self, scope: dict, receive: Any, send: Any, refusal: _Refusal) -> None:
        if scope["type"] == "websocket":
            # Accepted, then closed, so a browser sees the close code (§4.3).
            message = await receive()
            if message.get("type") != "websocket.connect":
                return
            await send({"type": "websocket.accept"})
            await send({"type": "websocket.close", "code": refusal.ws_code})
            return
        if refusal.status == 403:
            await _respond(send, 403, b"Forbidden", b"text/plain; charset=utf-8")
            return
        if refusal.status == 500:
            # Fixed text: a refusal body never carries what went wrong (§7.5).
            await _respond(send, 500, b"Internal Server Error", b"text/plain; charset=utf-8")
            return
        path = scope.get("path", "")
        if path == "/api" or path.startswith("/api/"):
            body = json.dumps({"error": "sign-in required"}).encode()
            await _respond(send, 401, body, b"application/json")
        elif self.policies.sign_in_url is not None:
            await _respond(send, 303, b"", b"text/plain; charset=utf-8",
                           [(b"location", self.policies.sign_in_url.encode("latin-1"))])
        else:
            await _respond(send, 401, b"Sign-in required", b"text/plain; charset=utf-8")


def _origin_matches(origin: str, host: str, port: int) -> bool:
    """Whether an ``Origin`` names the effective host and port (§7.4 rule 3)."""
    scheme, sep, rest = origin.partition("://")
    scheme = scheme.lower()
    if not sep or scheme not in ("http", "https"):
        return False
    parsed = normalise_host(rest)
    if parsed is None:
        return False
    origin_host, origin_port = parsed
    return (origin_host, origin_port or DEFAULT_PORTS[scheme]) == (host, port)


async def _respond(send: Any, status: int, body: bytes, content_type: bytes,
                   extra: list[tuple[bytes, bytes]] | None = None) -> None:
    headers = [(b"content-type", content_type), (b"content-length", str(len(body)).encode())]
    await send({"type": "http.response.start", "status": status,
                "headers": headers + (extra or [])})
    await send({"type": "http.response.body", "body": body})
