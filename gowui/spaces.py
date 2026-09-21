"""The space registry and its hubs (SPEC §3.1, §4.3, §8.2), the same in every launch mode.

The registry holds at most one live space per identity key. Getting a space, attaching a tab and
releasing a space take one lock per key. A space is loaded and restored in worker threads, then
published, then resumed in a task the registry owns. Each space has a hub whose synchronous
``broadcast`` is the session's: it serialises a frame once and puts the text into every tab's
bounded queue, drained by one sender task per tab. A newer ``state`` or ``analysis`` frame
supersedes a queued unsent one of its type, and a ``state``, ``analysis``, ``log`` or
``log_history`` that would overflow a queue first folds the queued logs into one ``log_history``,
so only other frames can overflow a queue; a fold that would leave the queue mostly unfoldable
closes the tab instead. Once a tab's page acknowledges a ``state`` (the ``ack`` frame), its
sender sends nothing while a ``state`` it sent is unacknowledged, so the frames wait where they
can still be superseded rather than piling up in transit. Saves are change-detected and
serialised per space; the write runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .policies import Identity, Policies
from .session import GameSession

__all__ = ["DEFAULT_QUEUE_SIZE", "MAX_TABS", "SAVE_INTERVAL", "Hub", "Space", "SpaceRegistry",
           "Tab", "TabQueue", "TooManyTabs"]

log = logging.getLogger("gowui")

#: Frames a tab's queue holds before the tab is closed with ``1013`` (§4.3).
DEFAULT_QUEUE_SIZE = 256
#: Open sockets one identity may hold at once (§4.3, §7.6).
MAX_TABS = 32
#: Seconds between autosave passes (§8.2).
SAVE_INTERVAL = 5.0
#: WebSocket close code for a tab that does not keep up (§4.3).
CLOSE_OVERFLOW = 1013
#: How long shutdown waits for a space's resume task after ``aclose()``.
RESUME_WAIT = 5.0

#: Unacknowledged ``state`` frames an acknowledging tab may have before its queue waits (§4.3).
STATE_WINDOW = 1
#: Frame types a newer frame of the same type supersedes in a tab's queue (§4.3 Coalescing).
COALESCED = frozenset({"state", "analysis"})
#: Frame types a ``log_history`` of the current history replaces on overflow (§4.3 Log folding).
FOLDED = frozenset({"log", "log_history"})
#: Frame types that fold the queued logs instead of being refused on overflow (§4.3).
FOLDING = COALESCED | FOLDED

Send = Callable[[str], Awaitable[None]]
Close = Callable[[int], Awaitable[None]]


class TooManyTabs(Exception):
    """An identity already holds :data:`MAX_TABS` sockets (§4.3); the route closes the new one."""


class TabQueue:
    """A tab's bounded frame queue (§4.3). A ``state`` or ``analysis`` put while an unsent frame
    of its type waits removes that frame and goes to the end; a ``state``, ``analysis``, ``log``
    or ``log_history`` that would overflow replaces every queued ``log`` and ``log_history`` with
    ``history()`` at the end (a ``log`` or ``log_history`` is folded into it, the others follow
    it), unless more than half the queue would still be unfoldable; other frames are only
    appended."""

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._items: deque[tuple[str | None, str]] = deque()
        self._ready = asyncio.Event()

    def __len__(self) -> int:
        return len(self._items)

    def put(self, kind: str | None, text: str,
            history: Callable[[], str | None] | None = None) -> bool:
        """Enqueue one frame's text; ``False`` when the queue is full."""
        if kind in COALESCED:
            for index, (queued, _) in enumerate(self._items):
                if queued == kind:
                    del self._items[index]
                    break
        if len(self._items) >= self.maxsize:
            if kind not in FOLDING or history is None:
                return False
            # A queue mostly of unfoldable frames (its own errors) would fold on every line.
            if sum(1 for item in self._items if item[0] not in FOLDED) > self.maxsize // 2:
                return False
            folded = history()
            if folded is None:
                return False
            self._items = deque(item for item in self._items if item[0] not in FOLDED)
            self._items.append(("log_history", folded))
            if kind in FOLDED:
                self._ready.set()
                return True
        self._items.append((kind, text))
        self._ready.set()
        return True

    async def get(self) -> tuple[str | None, str]:
        """The oldest queued frame's type and text, once there is one."""
        while not self._items:
            self._ready.clear()
            await self._ready.wait()
        return self._items.popleft()


class Tab:
    """One attached browser tab: its queue and sender task."""

    def __init__(self, space: "Space", send: Send, close: Close, queue_size: int) -> None:
        self.space = space
        self._send = send
        self._close = close
        self.queue = TabQueue(queue_size)
        self.sender: asyncio.Task | None = None
        #: Set once the registry has detached the tab.
        self.detached = False
        #: ``state`` frames sent and acknowledged; a tab is paced once it acknowledges (§4.3).
        self.states_sent = 0
        self.states_acked = 0
        self.acknowledging = False
        self._acked = asyncio.Event()

    def ack(self) -> None:
        """The page applied one more ``state`` (§4.1 ``ack``, §4.3 Acknowledgement)."""
        self.acknowledging = True
        if self.states_acked < self.states_sent:
            self.states_acked += 1
        self._acked.set()

    async def window(self) -> None:
        """Wait while an acknowledging tab has ``STATE_WINDOW`` unacknowledged ``state`` frames:
        frames wait in the queue meanwhile, where newer ones supersede or fold them."""
        while self.acknowledging and self.states_sent - self.states_acked >= STATE_WINDOW:
            self._acked.clear()
            await self._acked.wait()

    def push(self, frame: dict) -> None:
        """Send one frame to this tab only, in order with its broadcasts."""
        self.space.hub.deliver(self, json.dumps(frame), frame.get("type"))


class Hub:
    """Fans a space's frames out to its tabs (§4.3). ``broadcast`` never awaits."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.tabs: list[Tab] = []
        #: The session's current ``log_history`` frame as JSON text (encoded once and shared by
        #: every tab until the log changes), or ``None``; set by the registry and read
        #: synchronously when a frame would overflow a queue (§4.3 Log folding).
        self.history: Callable[[], str | None] | None = None
        self._clock = clock
        #: When the last tab left (or the hub was made); idle release measures from here.
        self.idle_since = clock()
        self._closing: set[asyncio.Task] = set()

    def broadcast(self, frame: dict) -> None:
        # With no tab attached nothing is touched: restore() broadcasts from a worker thread.
        if not self.tabs:
            return None
        text, kind = json.dumps(frame), frame.get("type")
        for tab in list(self.tabs):
            self.deliver(tab, text, kind)
        return None

    def deliver(self, tab: Tab, text: str, kind: str | None = None) -> None:
        if tab not in self.tabs:
            return
        if not tab.queue.put(kind, text, self._history_text):
            self.remove(tab)
            task = asyncio.ensure_future(_close_quietly(tab, CLOSE_OVERFLOW))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    def _history_text(self) -> str | None:
        return self.history() if self.history is not None else None

    def add(self, tab: Tab, frames: list[dict]) -> None:
        """Register ``tab`` and enqueue its attach frames in one step, with no ``await``."""
        self.tabs.append(tab)
        for frame in frames:
            self.deliver(tab, json.dumps(frame), frame.get("type"))
        if tab in self.tabs:
            tab.sender = asyncio.ensure_future(_drain(self, tab))

    def remove(self, tab: Tab) -> None:
        if tab in self.tabs:
            self.tabs.remove(tab)
            if not self.tabs:
                self.idle_since = self._clock()
        if tab.sender is not None and tab.sender is not asyncio.current_task():
            tab.sender.cancel()

    async def aclose(self) -> None:
        """Stop every sender task (shutdown and release)."""
        senders = [t.sender for t in self.tabs if t.sender is not None]
        for tab in list(self.tabs):
            self.remove(tab)
        await asyncio.gather(*senders, *self._closing, return_exceptions=True)


async def _drain(hub: Hub, tab: Tab) -> None:
    while True:
        await tab.window()
        kind, text = await tab.queue.get()
        if kind == "state":
            tab.states_sent += 1
        try:
            await tab._send(text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the socket is gone; the route detaches the tab
            hub.remove(tab)
            return


async def _close_quietly(tab: Tab, code: int) -> None:
    with contextlib.suppress(Exception):
        await tab._close(code)


@dataclass(eq=False)
class Space:
    """One identity's working state: its session and hub (§3.1)."""

    key: str
    session: GameSession
    hub: Hub
    #: The snapshot text last loaded or saved (§8.2 change detection).
    saved_text: str = ""
    #: The same for the preferences the storage keeps, ``""`` when it keeps none (§6.4, §8.2).
    saved_preferences_text: str = ""
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    resume_task: asyncio.Task | None = None


def _text(snapshot: dict) -> str:
    return json.dumps(snapshot)


@dataclass
class _KeyLock:
    """One key's lock and how many callers hold or wait for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class SpaceRegistry:
    """At most one live space per identity key (§3.1)."""

    def __init__(self, policies: Policies, *, save_interval: float = SAVE_INTERVAL,
                 queue_size: int = DEFAULT_QUEUE_SIZE, max_tabs: int = MAX_TABS,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policies = policies
        self.save_interval = save_interval
        self.queue_size = queue_size
        self.max_tabs = max_tabs
        self.clock = clock
        self.live: dict[str, Space] = {}
        self._locks: dict[str, _KeyLock] = {}
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._closed = False

    @contextlib.asynccontextmanager
    async def _keyed(self, key: str):
        """Hold that key's lock. The lock is counted while it is held or waited for and dropped
        once no one is either, so the table does not grow with every key ever used (an SSO name
        needs no account, §6.2)."""
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = _KeyLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0 and self._locks.get(key) is entry:
                del self._locks[key]

    # -- getting and attaching (§3.1, §4.3) ------------------------------------------------------
    async def get(self, identity: Identity) -> Space:
        async with self._keyed(identity.key):
            return await self._space(identity.key)

    async def attach(self, identity: Identity, send: Send, close: Close) -> Tab:
        """Attach one tab, or raise :class:`TooManyTabs` when the identity is at its cap (§4.3)."""
        async with self._keyed(identity.key):
            space = await self._space(identity.key)
            if len(space.hub.tabs) >= self.max_tabs:
                raise TooManyTabs(identity.key)
            tab = Tab(space, send, close, self.queue_size)
            space.hub.add(tab, space.session.attach_frames())
            return tab

    async def detach(self, tab: Tab) -> None:
        if tab.detached:
            return
        tab.detached = True
        space = tab.space
        space.hub.remove(tab)
        if not space.hub.tabs and self.live.get(space.key) is space:
            await self._save(space)

    async def _space(self, key: str) -> Space:
        space = self.live.get(key)
        if space is None:
            space = await self._create(key)
        return space

    def _new_session(self, hub: Hub, preferences: dict | None) -> GameSession:
        engines = self.policies.engines
        return GameSession(engines.resolve, expose_address=engines.expose_address,
                           broadcast=hub.broadcast, preferences=preferences)

    async def _preferences(self, key: str) -> dict | None:
        """What the storage keeps for ``key``, or ``None`` when it keeps none (§6.4)."""
        storage = self.policies.storage
        if not getattr(storage, "keeps_preferences", False):
            return None
        stored = await asyncio.to_thread(storage.load_preferences, key)
        return stored if isinstance(stored, dict) else {}

    async def _create(self, key: str) -> Space:
        if self._closed:
            raise RuntimeError("the space registry is closed")
        storage = self.policies.storage
        stored = await asyncio.to_thread(storage.load, key)
        preferences = await self._preferences(key)
        hub = Hub(self.clock)
        session = self._new_session(hub, preferences)
        if stored is not None:
            try:
                # A fresh session, restored once, off the loop and before it is published.
                await asyncio.to_thread(session.restore, stored)
            except Exception as exc:  # noqa: BLE001 - restore refused the snapshot (§8.1)
                await asyncio.to_thread(storage.set_aside, key,
                                        f"was refused by restore ({str(exc)[:200]})")
                session = self._new_session(hub, preferences)
        baseline = await asyncio.to_thread(lambda: _text(session.snapshot()))
        if self._closed:
            # Shut down while this space was being created: never publish it (§3.1 step 3).
            await session.aclose()
            raise RuntimeError("the space registry is closed")
        hub.history = session.log_history_text
        kept = session.preferences
        space = Space(key, session, hub, saved_text=baseline,
                      saved_preferences_text="" if kept is None else _text(kept))
        self.live[key] = space
        space.resume_task = asyncio.ensure_future(session.resume())
        space.resume_task.add_done_callback(_report_resume)
        return space

    # -- saving (§8.2) ----------------------------------------------------------------------------
    async def _save(self, space: Space) -> bool:
        """Save what changed — the snapshot, and the preferences the storage keeps (§6.4) —
        and report whether all of it is stored, which decides a release (§8.2)."""
        # Shielded: a cancelled caller never leaves a write running outside the save lock.
        return await asyncio.shield(self._save_now(space))

    async def _save_now(self, space: Space) -> bool:
        async with space.save_lock:
            stored = True
            snapshot = space.session.snapshot()
            text = _text(snapshot)
            if text != space.saved_text:
                try:
                    await asyncio.to_thread(self.policies.storage.save, space.key, snapshot)
                except Exception as exc:  # noqa: BLE001 - try again on the next pass
                    log.warning("gowui: could not save the space %r: %s", space.key, exc)
                    stored = False
                else:
                    space.saved_text = text
            # The preferences the storage keeps ride the same pass and comparison (§6.4, §8.2).
            preferences = space.session.preferences
            if preferences is not None:
                kept = _text(preferences)
                if kept != space.saved_preferences_text:
                    try:
                        await asyncio.to_thread(self.policies.storage.save_preferences,
                                                space.key, preferences)
                    except Exception as exc:  # noqa: BLE001 - try again on the next pass
                        log.warning("gowui: could not save the preferences of %r: %s",
                                    space.key, exc)
                        stored = False
                    else:
                        space.saved_preferences_text = kept
            return stored

    async def save_changed(self) -> None:
        """One autosave pass: save every space that changed."""
        for space in list(self.live.values()):
            await self._save(space)

    async def sweep_storage(self) -> None:
        """Let the storage policy do its own periodic work (§8.2) — server mode purges the
        expired logins of §7.2 here. A storage without ``sweep`` has none."""
        sweep = getattr(self.policies.storage, "sweep", None)
        if sweep is None:
            return
        try:
            await asyncio.to_thread(sweep)
        except Exception as exc:  # noqa: BLE001 - the next pass tries again
            log.warning("gowui: the storage sweep failed: %s", exc)

    # -- idle release (§7.8, §8.2) ------------------------------------------------------------------
    def _idle(self, space: Space, seconds: float) -> bool:
        return not space.hub.tabs and self.clock() - space.hub.idle_since >= seconds

    async def release_idle(self) -> None:
        """One sweep: save, close and drop every space idle for ``idle_release_seconds``."""
        seconds = self.policies.idle_release_seconds
        if seconds is None:
            return
        for key, space in list(self.live.items()):
            if not self._idle(space, seconds):
                continue
            async with self._keyed(key):
                if self.live.get(key) is not space or not self._idle(space, seconds):
                    continue
                if not await self._save(space):
                    continue  # a failed save keeps the space live; a later sweep retries
                try:
                    await self._close_space(space)
                finally:
                    del self.live[key]

    async def _close_space(self, space: Space) -> None:
        await space.session.aclose()
        await space.hub.aclose()
        task = space.resume_task
        if task is not None and not task.done():
            _, pending = await asyncio.wait({task}, timeout=RESUME_WAIT)
            for waiting in pending:
                waiting.cancel()

    # -- the periodic task and shutdown -------------------------------------------------------------
    async def start(self) -> None:
        """Start the periodic autosave and idle-release task."""
        if self._task is None and not self._closed:
            self._stop = asyncio.Event()
            self._task = asyncio.ensure_future(self._run(self._stop))

    async def _run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(stop.wait(), self.save_interval)
            if stop.is_set():
                return
            try:
                await self.save_changed()
                await self.release_idle()
                await self.sweep_storage()
            except Exception:  # noqa: BLE001 - the next pass tries again
                log.exception("gowui: the autosave pass failed")

    async def aclose(self) -> None:
        """Shutdown: stop the periodic task, then save and close every space. Nothing cancels
        a session's ``aclose()``."""
        if self._closed:
            return
        self._closed = True
        if self._task is not None and self._stop is not None:
            self._stop.set()
            await self._task

        async def shut(key: str, space: Space) -> None:
            async with self._keyed(key):
                try:
                    await self._save(space)
                    await self._close_space(space)
                finally:
                    if self.live.get(key) is space:
                        del self.live[key]

        await asyncio.gather(*(shut(k, s) for k, s in list(self.live.items())),
                             return_exceptions=True)


def _report_resume(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        log.warning("gowui: resuming a space failed: %r", task.exception())
