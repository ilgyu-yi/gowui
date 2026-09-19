"""The space registry and its hubs (SPEC §3.1, §4.3, §8.2), the same in every launch mode.

The registry holds at most one live space per identity key. Getting a space, attaching a tab and
releasing a space take one lock per key. A space is loaded and restored in worker threads, then
published, then resumed in a task the registry owns. Each space has a hub whose synchronous
``broadcast`` is the session's: it serialises a frame once and puts the text into every tab's
bounded queue, drained by one sender task per tab. Saves are change-detected and serialised per
space; the write runs in a worker thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .policies import Identity, Policies
from .session import GameSession

__all__ = ["DEFAULT_QUEUE_SIZE", "SAVE_INTERVAL", "Hub", "Space", "SpaceRegistry", "Tab"]

log = logging.getLogger("gowui")

#: Frames a tab's queue holds before the tab is closed with ``1013`` (§4.3).
DEFAULT_QUEUE_SIZE = 256
#: Seconds between autosave passes (§8.2).
SAVE_INTERVAL = 5.0
#: WebSocket close code for a tab that does not keep up (§4.3).
CLOSE_OVERFLOW = 1013
#: How long shutdown waits for a space's resume task after ``aclose()``.
RESUME_WAIT = 5.0

Send = Callable[[str], Awaitable[None]]
Close = Callable[[int], Awaitable[None]]


class Tab:
    """One attached browser tab: its queue and sender task."""

    def __init__(self, space: "Space", send: Send, close: Close, queue_size: int) -> None:
        self.space = space
        self._send = send
        self._close = close
        self.queue: asyncio.Queue[str] = asyncio.Queue(queue_size)
        self.sender: asyncio.Task | None = None
        #: Set once the registry has detached the tab.
        self.detached = False

    def push(self, frame: dict) -> None:
        """Send one frame to this tab only, in order with its broadcasts."""
        self.space.hub.deliver(self, json.dumps(frame))


class Hub:
    """Fans a space's frames out to its tabs (§4.3). ``broadcast`` never awaits."""

    def __init__(self, clock: Callable[[], float]) -> None:
        self.tabs: list[Tab] = []
        self._clock = clock
        #: When the last tab left (or the hub was made); idle release measures from here.
        self.idle_since = clock()
        self._closing: set[asyncio.Task] = set()

    def broadcast(self, frame: dict) -> None:
        # With no tab attached nothing is touched: restore() broadcasts from a worker thread.
        if not self.tabs:
            return None
        text = json.dumps(frame)
        for tab in list(self.tabs):
            self.deliver(tab, text)
        return None

    def deliver(self, tab: Tab, text: str) -> None:
        if tab not in self.tabs:
            return
        try:
            tab.queue.put_nowait(text)
        except asyncio.QueueFull:
            self.remove(tab)
            task = asyncio.ensure_future(_close_quietly(tab, CLOSE_OVERFLOW))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)

    def add(self, tab: Tab, frames: list[dict]) -> None:
        """Register ``tab`` and enqueue its attach frames in one step, with no ``await``."""
        self.tabs.append(tab)
        for frame in frames:
            self.deliver(tab, json.dumps(frame))
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
        text = await tab.queue.get()
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
    save_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    resume_task: asyncio.Task | None = None


def _text(snapshot: dict) -> str:
    return json.dumps(snapshot)


class SpaceRegistry:
    """At most one live space per identity key (§3.1)."""

    def __init__(self, policies: Policies, *, save_interval: float = SAVE_INTERVAL,
                 queue_size: int = DEFAULT_QUEUE_SIZE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.policies = policies
        self.save_interval = save_interval
        self.queue_size = queue_size
        self.clock = clock
        self.live: dict[str, Space] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None
        self._stop: asyncio.Event | None = None
        self._closed = False

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # -- getting and attaching (§3.1, §4.3) ------------------------------------------------------
    async def get(self, identity: Identity) -> Space:
        async with self._lock(identity.key):
            return await self._space(identity.key)

    async def attach(self, identity: Identity, send: Send, close: Close) -> Tab:
        async with self._lock(identity.key):
            space = await self._space(identity.key)
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

    def _new_session(self, hub: Hub) -> GameSession:
        engines = self.policies.engines
        return GameSession(engines.resolve, expose_address=engines.expose_address,
                           broadcast=hub.broadcast)

    async def _create(self, key: str) -> Space:
        if self._closed:
            raise RuntimeError("the space registry is closed")
        storage = self.policies.storage
        stored = await asyncio.to_thread(storage.load, key)
        hub = Hub(self.clock)
        session = self._new_session(hub)
        if stored is not None:
            try:
                # A fresh session, restored once, off the loop and before it is published.
                await asyncio.to_thread(session.restore, stored)
            except Exception as exc:  # noqa: BLE001 - restore refused the snapshot (§8.1)
                await asyncio.to_thread(storage.set_aside, key,
                                        f"was refused by restore ({str(exc)[:200]})")
                session = self._new_session(hub)
        baseline = await asyncio.to_thread(lambda: _text(session.snapshot()))
        if self._closed:
            # Shut down while this space was being created: never publish it (§3.1 step 3).
            await session.aclose()
            raise RuntimeError("the space registry is closed")
        space = Space(key, session, hub, saved_text=baseline)
        self.live[key] = space
        space.resume_task = asyncio.ensure_future(session.resume())
        space.resume_task.add_done_callback(_report_resume)
        return space

    # -- saving (§8.2) ----------------------------------------------------------------------------
    async def _save(self, space: Space) -> bool:
        """Save the space if it changed; whether its current snapshot is stored (§8.2)."""
        # Shielded: a cancelled caller never leaves a write running outside the save lock.
        return await asyncio.shield(self._save_now(space))

    async def _save_now(self, space: Space) -> bool:
        async with space.save_lock:
            snapshot = space.session.snapshot()
            text = _text(snapshot)
            if text == space.saved_text:
                return True
            try:
                await asyncio.to_thread(self.policies.storage.save, space.key, snapshot)
            except Exception as exc:  # noqa: BLE001 - try again on the next pass
                log.warning("gowui: could not save the space %r: %s", space.key, exc)
                return False
            space.saved_text = text
            return True

    async def save_changed(self) -> None:
        """One autosave pass: save every space that changed."""
        for space in list(self.live.values()):
            await self._save(space)

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
            async with self._lock(key):
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
            async with self._lock(key):
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
