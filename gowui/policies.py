"""The policy bundle a launch mode is made of (SPEC §6.1, §6.2).

A launch mode is data: one :class:`Policies` value holding an identity policy, an engine-address
policy, a storage policy and a few plain fields. The app factory, the routes, the guard and the
space registry read only this bundle; none of them receives or compares a mode name.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network
from typing import Any, Protocol

from .session import EngineTarget

__all__ = ["EnginePolicy", "Identity", "IdentityPolicy", "Policies", "StoragePolicy"]


@dataclass(frozen=True)
class Identity:
    """Who a request or handshake belongs to (§6.2); ``key`` names the space."""

    key: str
    name: str = ""
    #: How it was established: ``none``, ``local`` (password) or ``sso``.
    source: str = "none"
    #: The log-out offered: ``""`` (none), ``local`` or ``sso``.
    logout_kind: str = ""
    logout_url: str = ""


class IdentityPolicy(Protocol):
    """Resolves a request or handshake to an identity, and answers the sign-in routes (§6.2)."""

    def identify(self, conn: Any) -> Identity | None: ...

    async def login_page(self, request: Any) -> Any: ...

    async def login(self, request: Any) -> Any: ...

    async def logout(self, request: Any) -> Any: ...


class EnginePolicy(Protocol):
    """Validates and resolves an engine request (§6.3)."""

    #: Whether engine addresses may be shown to a browser (§7.7).
    expose_address: bool
    #: Whether the policy offers the console for any engine (``console`` of ``/api/health``).
    offers_console: bool

    def resolve(self, request: Any) -> EngineTarget: ...

    def describe(self) -> dict: ...


class StoragePolicy(Protocol):
    """Loads and saves a space's snapshot by key; synchronous, called in a worker thread (§6.4).

    ``keeps_preferences`` says whether the policy also keeps the identity's preferences — the UI
    language and the tuple presets (§4.1). Only a policy that does needs ``load_preferences`` and
    ``save_preferences``; a storage without the field keeps none, and the page then keeps both in
    the browser (§8.5).

    A policy may also offer ``sweep()``, its own periodic work, which the registry's save pass
    calls in a worker thread (§8.2): server mode purges expired logins there (§7.2). A policy
    without the method has none, and the pass does nothing for it.
    """

    keeps_preferences: bool

    def load(self, key: str) -> dict | None: ...

    def save(self, key: str, snapshot: dict) -> None: ...

    def set_aside(self, key: str, reason: str) -> None: ...

    def load_preferences(self, key: str) -> dict | None: ...

    def save_preferences(self, key: str, preferences: dict) -> None: ...


@dataclass(frozen=True)
class Policies:
    """The policy bundle (§6.1)."""

    identity: IdentityPolicy
    engines: EnginePolicy
    storage: StoragePolicy
    #: Release a space after this long with no tab; ``None`` never releases (§8.2).
    idle_release_seconds: float | None = None
    #: Normalised host names the Host rule accepts; ``None`` accepts any (§7.4).
    allowed_hosts: frozenset[str] | None = None
    #: Whether any IP literal also passes the Host rule (§7.4).
    allow_ip_literals: bool = False
    #: Peers whose forwarded headers are believed (§7.3, §7.10).
    trusted_proxies: tuple[IPv4Network | IPv6Network, ...] = ()
    #: Where a page request without an identity is sent; ``None`` gives a ``401`` page (§5).
    sign_in_url: str | None = None
