"""The policies of local mode (SPEC §6.1–§6.4, §8.3): one fixed owner, typed engine addresses, and
a per-user JSON state file (or memory only).

Only the command line builds this bundle (§9); nothing below it knows which mode it runs in.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import logging
import os
import tempfile
import time
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from starlette.responses import RedirectResponse, Response

from .engine import PROTOCOLS
from .guard import normalise_host
from .policies import Identity, Policies
from .session import EngineRequestError, EngineTarget

__all__ = ["JsonFileStorage", "LocalIdentity", "MemoryStorage", "STATE_READ_CAP",
           "TypedAddresses", "default_state_path", "local_policies"]

log = logging.getLogger("gowui")

MIB = 1024 * 1024
#: 64 boards × 1 MiB of SGF × 6 (worst-case ASCII-escaped JSON growth) + 1 MiB (§8.3).
STATE_READ_CAP = 64 * MIB * 6 + MIB
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})
DEFAULT_ENGINE = {"protocol": "gtp", "host": "127.0.0.1", "port": 6363}
MAX_ENGINE_HOST = 253


class LocalIdentity:
    """Every request is the owner; no sign-in, no cookie (§6.2)."""

    OWNER = Identity(key="owner", name="", source="none", logout_kind="", logout_url="")

    def identify(self, conn: Any) -> Identity:
        return self.OWNER

    async def login_page(self, request: Any) -> Response:
        return RedirectResponse("/", status_code=303)

    async def login(self, request: Any) -> Response:
        return RedirectResponse("/", status_code=303)

    async def logout(self, request: Any) -> Response:
        return RedirectResponse("/", status_code=303)


class TypedAddresses:
    """The browser types ``{protocol, host, port}``; the CLI flags are the defaults (§6.3)."""

    expose_address = True
    offers_console = True

    def __init__(self, defaults: Mapping[str, Any]) -> None:
        self.defaults = dict(defaults)

    def resolve(self, request: Any) -> EngineTarget:
        if not isinstance(request, dict):
            raise EngineRequestError("an engine request must be an object")
        fields = {k: v for k, v in request.items() if k != "type"}
        if set(fields) - {"protocol", "host", "port"}:
            raise EngineRequestError("an engine request has protocol, host and port only")
        protocol, host, port = fields.get("protocol"), fields.get("host"), fields.get("port")
        if protocol not in PROTOCOLS:
            raise EngineRequestError(f"the protocol must be one of {', '.join(PROTOCOLS)}")
        if (not isinstance(host, str) or not host or len(host) > MAX_ENGINE_HOST
                or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in host)):
            raise EngineRequestError(
                "the host must be 1 to 253 characters without spaces or control characters")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise EngineRequestError("the port must be a whole number from 1 to 65535")
        return EngineTarget(protocol=protocol, host=host, port=port,
                            request_echo={"protocol": protocol, "host": host, "port": port},
                            console=True)

    def describe(self) -> dict:
        return {"kind": "typed", "defaults": dict(self.defaults)}


class MemoryStorage:
    """Snapshots in memory only; never touches a file (``--fresh``, §6.4)."""

    def __init__(self) -> None:
        self._data: dict[str, dict] = {}

    def load(self, key: str) -> dict | None:
        value = self._data.get(key)
        return None if value is None else copy.deepcopy(value)

    def save(self, key: str, snapshot: dict) -> None:
        self._data[key] = copy.deepcopy(snapshot)

    def set_aside(self, key: str, reason: str) -> None:
        if self._data.pop(key, None) is not None:
            log.warning("gowui: a stored snapshot was dropped: %s", reason)


class JsonFileStorage:
    """One JSON state file (§8.3). Local mode has one space, so the key is not part of the path."""

    def __init__(self, path: str | os.PathLike, *, max_bytes: int = STATE_READ_CAP) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes

    def load(self, key: str) -> dict | None:
        try:
            with open(self.path, "rb") as file:
                data = file.read(self.max_bytes + 1)
        except FileNotFoundError:
            return None
        except OSError as exc:
            self._set_aside(f"cannot be read ({exc.strerror or type(exc).__name__})")
            return None
        if len(data) > self.max_bytes:
            self._set_aside(f"is larger than {self.max_bytes} bytes")
            return None
        try:
            value = json.loads(data.decode("utf-8"))
        except UnicodeDecodeError:
            self._set_aside("is not UTF-8 text")
            return None
        except (ValueError, RecursionError):
            self._set_aside("is not valid JSON")
            return None
        if not isinstance(value, dict):
            self._set_aside("is not a JSON object")
            return None
        return value

    def save(self, key: str, snapshot: dict) -> None:
        text = json.dumps(snapshot, ensure_ascii=True)
        directory = self.path.parent
        _make_private_dirs(directory)
        fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp",
                                         dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="ascii") as file:
                file.write(text)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    def set_aside(self, key: str, reason: str) -> None:
        self._set_aside(reason)

    def _set_aside(self, reason: str) -> None:
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        base = f"{self.path.name}.bad-{stamp}"
        target = self.path.with_name(base)
        suffix = 2
        while os.path.lexists(target):
            target = self.path.with_name(f"{base}-{suffix}")
            suffix += 1
        try:
            os.rename(self.path, target)
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("gowui: the state file %s %s and could not be set aside (%s)",
                        self.path, reason, exc.strerror or type(exc).__name__)
            return
        log.warning("gowui: the state file %s %s; it was set aside as %s and gowui starts fresh",
                    self.path, reason, target)


def _make_private_dirs(directory: Path) -> None:
    """Create ``directory`` and any missing parent with mode 0700; existing ones are unchanged."""
    missing: list[Path] = []
    current = directory
    while not current.exists():
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    for path in reversed(missing):
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            continue
        if os.name != "nt":
            os.chmod(path, 0o700)


def default_state_path(platform: str, env: Mapping[str, str], home: Path) -> Path:
    """The per-user state file for ``sys.platform`` (§8.3)."""
    if platform == "darwin":
        base = Path(home) / "Library" / "Application Support"
    elif platform == "win32":
        local = env.get("LOCALAPPDATA") or ""
        base = Path(local) if local else Path(home) / "AppData" / "Local"
    else:
        xdg = env.get("XDG_STATE_HOME") or ""
        base = Path(xdg) if xdg and os.path.isabs(xdg) else Path(home) / ".local" / "state"
    return base / "gowui" / "state.json"


def _host_name(value: str) -> str | None:
    """The ``--host`` value as the Host rule compares it: an IP literal or a normalised name."""
    bare = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        return ipaddress.ip_address(bare).compressed.lower()
    except ValueError:
        pass
    parsed = normalise_host(bare)
    return None if parsed is None else parsed[0]


def local_policies(*, host: str = "127.0.0.1", storage: Any,
                   engine_defaults: Mapping[str, Any] | None = None) -> Policies:
    """The local bundle (§6.1): the owner, typed addresses, the given storage."""
    allowed = set(LOOPBACK_NAMES)
    name = _host_name(host)
    if name:
        allowed.add(name)
    return Policies(
        identity=LocalIdentity(),
        engines=TypedAddresses(engine_defaults or DEFAULT_ENGINE),
        storage=storage,
        idle_release_seconds=None,
        allowed_hosts=frozenset(allowed),
        allow_ip_literals=True,
        trusted_proxies=(),
        sign_in_url=None,
    )
