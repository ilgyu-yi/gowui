"""The space registry and its hubs (SPEC §3.1, §4.3, §6.4, §8.2): one live space per identity key,
created in four steps (load and restore in worker threads, publish, resume on the loop); tabs
attached through a per-space hub with bounded per-tab queues; change-detected, serialised saves;
idle release under the same per-key lock as attach; and shutdown.

The registry is driven directly with an injected clock and save interval; ``save_changed()`` and
``release_idle()`` are the ticks the periodic task runs, so no test waits on the wall clock for
them. Tabs are plain ``send`` / ``close`` coroutines.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import inspect
import json
import threading
import warnings

import pytest

from app_helpers import LOOPBACK, local_bundle, snapshot_with
from helpers import HANG, wait_for
from session_helpers import gtp_count, settle


def owner():
    from gowui.policies import Identity

    return Identity(key="owner", name="", source="none", logout_kind="", logout_url="")


def identity(key: str):
    from gowui.policies import Identity

    return Identity(key=key, name=key, source="local", logout_kind="local", logout_url="")


class Storage:
    """A storage policy (§6.4) that remembers every call and the thread it ran in.

    With :attr:`gate` cleared, ``save`` blocks (in its worker thread) until the test sets it;
    :attr:`saving` is set as soon as a save has started.
    """

    def __init__(self, stored: dict | None = None) -> None:
        self.data: dict[str, str] = {k: json.dumps(v) for k, v in (stored or {}).items()}
        self.saves: list[tuple[str, dict]] = []
        self.loads: list[str] = []
        self.set_asides: list[tuple[str, str]] = []
        self.threads: list[tuple[str, threading.Thread]] = []
        self.gate = threading.Event()
        self.gate.set()
        self.saving = threading.Event()

    def load(self, key):
        self.threads.append(("load", threading.current_thread()))
        self.loads.append(key)
        text = self.data.get(key)
        return None if text is None else json.loads(text)

    def save(self, key, snapshot):
        self.threads.append(("save", threading.current_thread()))
        self.saving.set()
        assert self.gate.wait(HANG), "the test never released the save"
        self.saves.append((key, copy.deepcopy(snapshot)))
        self.data[key] = json.dumps(snapshot)

    def set_aside(self, key, reason):
        self.set_asides.append((key, reason))
        self.data.pop(key, None)

    def stored(self, key: str = "owner") -> dict | None:
        text = self.data.get(key)
        return None if text is None else json.loads(text)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeTab:
    """What the WebSocket route hands the registry: async ``send(text)`` and ``close(code)``."""

    def __init__(self, stuck: bool = False) -> None:
        self.texts: list[str] = []
        self.closed: list[int] = []
        self.stuck = stuck
        self._never = asyncio.Event()

    async def send(self, text: str) -> None:
        if self.stuck:
            await self._never.wait()
        self.texts.append(text)

    async def close(self, code: int = 1000) -> None:
        self.closed.append(code)

    @property
    def frames(self) -> list[dict]:
        return [json.loads(t) for t in self.texts]

    def types(self) -> list[str]:
        return [f.get("type") for f in self.frames]


@pytest.fixture
async def registries():
    """``make(storage=None, *, idle=None, **kwargs)`` builds a registry over the local bundle;
    every registry is shut down at teardown."""
    import dataclasses

    from gowui.spaces import SpaceRegistry

    made = []

    def make(storage=None, *, idle: float | None = None, **kwargs):
        policies = dataclasses.replace(local_bundle(storage if storage is not None else Storage()),
                                       idle_release_seconds=idle)
        registry = SpaceRegistry(policies, **kwargs)
        made.append(registry)
        return registry

    yield make
    for registry in made:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(registry.aclose(), 15)


async def play(space, vertex: str = "D4") -> None:
    game = space.session.state_message()["game"]
    await asyncio.wait_for(space.session.handle(
        {"type": "play", "color": game["toPlay"], "vertex": vertex}), HANG)


# -- defaults ----------------------------------------------------------------------------------------
def test_the_default_hub_queue_holds_256_frames():
    from gowui.spaces import DEFAULT_QUEUE_SIZE

    assert DEFAULT_QUEUE_SIZE == 256


def test_the_default_save_interval_is_five_seconds():
    from gowui.spaces import SAVE_INTERVAL

    assert SAVE_INTERVAL == 5.0


# -- one live space per key (§3.1) ---------------------------------------------------------------
async def test_getting_the_owner_twice_gives_the_same_space(registries):
    registry = registries()
    first = await registry.get(owner())
    assert await registry.get(owner()) is first


async def test_concurrent_gets_for_one_key_create_one_space(registries):
    storage = Storage()
    registry = registries(storage)
    first, second = await asyncio.gather(registry.get(owner()), registry.get(owner()))
    assert (first is second, storage.loads) == (True, ["owner"])


async def test_two_identities_get_two_spaces(registries):
    registry = registries()
    alice, bob = await registry.get(identity("alice")), await registry.get(identity("bob"))
    assert (alice is not bob, alice.key, bob.key) == (True, "alice", "bob")


async def test_each_space_is_loaded_under_its_own_key(registries):
    storage = Storage()
    registry = registries(storage)
    await registry.get(identity("alice"))
    await registry.get(identity("bob"))
    assert storage.loads == ["alice", "bob"]


async def test_a_published_space_is_listed_under_its_key(registries):
    registry = registries()
    space = await registry.get(owner())
    assert dict(registry.live) == {"owner": space}


async def test_a_stored_snapshot_is_restored_into_the_space(registries):
    registry = registries(Storage({"owner": snapshot_with("D4", "E5", name="study")}))
    space = await registry.get(owner())
    state = space.session.state_message()
    assert (state["game"]["moveCount"], state["boards"][0]["name"]) == (2, "study")


async def test_the_stored_snapshot_is_loaded_in_a_worker_thread(registries):
    storage = Storage({"owner": snapshot_with("D4")})
    registry = registries(storage)
    await registry.get(owner())
    loads = [thread for kind, thread in storage.threads if kind == "load"]
    assert loads and threading.main_thread() not in loads


async def test_the_session_is_restored_in_a_worker_thread(registries, monkeypatch):
    from gowui.session import GameSession

    threads = []
    real = GameSession.restore

    def restore(self, data):
        threads.append(threading.current_thread())
        return real(self, data)

    monkeypatch.setattr(GameSession, "restore", restore)
    registry = registries(Storage({"owner": snapshot_with("D4")}))
    await registry.get(owner())
    assert threads and threading.main_thread() not in threads


async def test_restoring_from_a_worker_thread_raises_no_loop_error_or_warning(registries):
    """``restore()`` broadcasts one ``state`` frame from its worker thread (§3.1 step 2); the hub
    must take it without touching the event loop from that thread."""
    loop = asyncio.get_running_loop()
    contexts: list[dict] = []
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            registry = registries(Storage({"owner": snapshot_with("D4", "E5")}))
            space = await registry.get(owner())
            tab = FakeTab()
            await registry.attach(owner(), tab.send, tab.close)
            await wait_for(lambda: len(tab.texts) >= 2)
            await play(space, "F6")
            await settle(0.2)
    finally:
        loop.set_exception_handler(previous)
    problems = [str(c.get("message")) for c in contexts] + [
        f"{w.category.__name__}: {w.message}" for w in caught
        if issubclass(w.category, (RuntimeWarning, ResourceWarning, DeprecationWarning))]
    assert problems == []


async def test_a_snapshot_restore_refuses_is_set_aside(registries):
    storage = Storage({"owner": {**snapshot_with("D4"), "version": 2}})
    registry = registries(storage)
    await registry.get(owner())
    assert [key for key, _ in storage.set_asides] == ["owner"]


async def test_a_space_whose_snapshot_was_refused_starts_fresh(registries):
    registry = registries(Storage({"owner": {**snapshot_with("D4"), "version": 2}}))
    space = await registry.get(owner())
    assert space.session.state_message()["game"]["moveCount"] == 0


async def test_getting_a_space_does_not_wait_for_the_engine_handshake(registries, fake_engine):
    server = await fake_engine("gtp", delay={"list_commands": 3.0})
    registry = registries(Storage({"owner": snapshot_with("D4", connected=True, request={
        "protocol": "gtp", "host": LOOPBACK, "port": server.port})}))
    space = await asyncio.wait_for(registry.get(owner()), 1.5)
    assert space.session.state_message()["engine"]["connected"] is False


async def test_a_restored_connected_space_reconnects_once(registries, gtp_server):
    registry = registries(Storage({"owner": snapshot_with("D4", connected=True, request={
        "protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})}))
    space = await registry.get(owner())
    await wait_for(lambda: space.session.state_message()["engine"]["connected"])
    await registry.get(owner())
    await settle(0.5)
    assert gtp_count(gtp_server, "list_commands") == 1


# -- the hub (§4.3) ------------------------------------------------------------------------------
async def test_a_tab_gets_state_then_log_history_when_it_attaches(registries):
    registry = registries()
    tab = FakeTab()
    await registry.attach(owner(), tab.send, tab.close)
    await wait_for(lambda: len(tab.texts) >= 2)
    assert tab.types()[:2] == ["state", "log_history"]


async def test_attaching_a_tab_gets_the_space_of_its_identity(registries):
    registry = registries()
    tab = FakeTab()
    attached = await registry.attach(identity("alice"), tab.send, tab.close)
    assert attached.space is registry.live["alice"]


async def test_a_broadcast_during_attach_reaches_the_tab_once_after_its_frames(registries):
    """Registering the tab and enqueueing its attach frames is one step with no await (§4.3): a
    broadcast scheduled while the attach frames are taken reaches the tab exactly once."""
    registry = registries()
    space = await registry.get(owner())
    loop = asyncio.get_running_loop()
    real = space.session.attach_frames
    marker = {"type": "log", "line": {"direction": "note", "text": "marker", "at": 0}}

    def attach_frames():
        frames = real()
        loop.call_soon(space.hub.broadcast, marker)
        return frames

    space.session.attach_frames = attach_frames
    tab = FakeTab()
    await registry.attach(owner(), tab.send, tab.close)
    await settle(0.3)
    markers = [f for f in tab.frames if f.get("type") == "log" and f["line"]["text"] == "marker"]
    assert (tab.types()[:2], len(markers)) == (["state", "log_history"], 1)


async def test_broadcast_is_synchronous(registries):
    registry = registries()
    space = await registry.get(owner())
    result = space.hub.broadcast({"type": "log", "line": {"direction": "note", "text": "x",
                                                          "at": 0}})
    assert result is None and not inspect.isawaitable(result)


async def test_a_session_change_reaches_every_attached_tab(registries):
    registry = registries()
    tabs = [FakeTab(), FakeTab()]
    for tab in tabs:
        await registry.attach(owner(), tab.send, tab.close)
    await play(registry.live["owner"])
    reached = [await wait_for(lambda t=t: any(f.get("type") == "state"
                                              and f["game"]["moveCount"] == 1
                                              for f in t.frames)) for t in tabs]
    assert reached == [True, True]


async def test_a_tab_is_sent_frames_in_broadcast_order(registries):
    registry = registries()
    tab = FakeTab()
    await registry.attach(owner(), tab.send, tab.close)
    space = registry.live["owner"]
    for index in range(20):
        space.hub.broadcast({"type": "log", "line": {"direction": "note", "text": str(index),
                                                     "at": 0}})
    await wait_for(lambda: len([f for f in tab.frames if f.get("type") == "log"]) >= 20)
    texts = [f["line"]["text"] for f in tab.frames if f.get("type") == "log"]
    assert texts == [str(i) for i in range(20)]


def note(text: str) -> dict:
    return {"type": "log", "line": {"direction": "note", "text": text, "at": 0}}


def error(text: str) -> dict:
    """A frame that is neither coalesced nor folded (§4.3), so it is what still overflows a
    queue: ``state``/``analysis`` are coalesced and ``log`` frames fold into a ``log_history``."""
    return {"type": "error", "message": text}


async def test_a_tab_whose_queue_overflows_is_closed_with_1013(registries):
    registry = registries(queue_size=4)
    stuck = FakeTab(stuck=True)
    await registry.attach(owner(), stuck.send, stuck.close)
    space = registry.live["owner"]
    for index in range(20):
        space.hub.broadcast(error(str(index)))
    await wait_for(lambda: stuck.closed)
    assert stuck.closed == [1013]


async def stalled(registry) -> tuple[FakeTab, object]:
    """A tab whose sender is stuck sending its first attach frame, and its space."""
    tab = FakeTab(stuck=True)
    await registry.attach(owner(), tab.send, tab.close)
    await settle(0.05)
    return tab, registry.live["owner"]


def unstick(tab: FakeTab) -> None:
    tab.stuck = False
    tab._never.set()


def marked(tab: FakeTab) -> list[dict]:
    """The frames the test broadcast (each carries ``n``), in the order the tab got them."""
    return [f for f in tab.frames if "n" in f or f.get("type") == "log"
            and f["line"]["text"].startswith("t")]


async def test_a_stalled_tab_is_not_closed_by_state_and_analysis_frames(registries):
    """§4.3 Coalescing: a newer ``state`` or ``analysis`` supersedes the queued one, so a tab
    that does not read never overflows on them; it gets the newest of each and every log line."""
    registry = registries(queue_size=8)
    tab, space = await stalled(registry)
    for n in range(500):
        space.hub.broadcast({"type": "state", "n": n})
        space.hub.broadcast({"type": "analysis", "cursor": n, "n": n})
        if n % 100 == 0:
            space.hub.broadcast(note(f"t{n}"))
    await settle(0.1)
    assert tab.closed == []
    unstick(tab)
    await wait_for(lambda: any(f.get("type") == "analysis" and f.get("n") == 499
                               for f in tab.frames))
    got = marked(tab)
    assert [f for f in got if f["type"] == "state"] == [{"type": "state", "n": 499}]
    assert [f["n"] for f in got if f["type"] == "analysis"] == [499]
    assert [f["line"]["text"] for f in got if f["type"] == "log"] == [
        "t0", "t100", "t200", "t300", "t400"]
    assert (tab.closed, tab.types().count("log_history")) == ([], 1)


async def acknowledging(registry) -> tuple[FakeTab, object, object]:
    """A tab that has received its attach frames and acknowledged the attach ``state``."""
    fake = FakeTab()
    tab = await registry.attach(owner(), fake.send, fake.close)
    await wait_for(lambda: len(fake.texts) >= 2)
    tab.ack()
    return fake, tab, registry.live["owner"]


async def test_an_acknowledging_tab_is_sent_nothing_while_a_state_is_unacknowledged(registries):
    """§4.3 "Acknowledgement": the frames wait in the queue, where the newest ``state``
    supersedes the others; each ``ack`` lets the frames up to the next ``state`` go, in order."""
    registry = registries(queue_size=8)
    fake, tab, space = await acknowledging(registry)
    space.hub.broadcast({"type": "state", "n": 0})
    await wait_for(lambda: any(f.get("n") == 0 for f in fake.frames))
    for n in range(1, 50):
        space.hub.broadcast({"type": "state", "n": n})
    space.hub.broadcast(note("t-after"))
    await settle(0.1)
    assert [f.get("n") for f in marked(fake)] == [0]
    tab.ack()
    await wait_for(lambda: any(f.get("n") == 49 for f in fake.frames))
    await settle(0.1)
    assert [f.get("n") for f in marked(fake)] == [0, 49]
    tab.ack()
    await wait_for(lambda: fake.types()[-1] == "log")
    assert [f.get("n", f.get("line", {}).get("text")) for f in marked(fake)] == [0, 49, "t-after"]
    assert fake.closed == []


async def test_an_ack_beyond_the_states_sent_opens_no_extra_window(registries):
    """§4.3 "Acknowledgement": an ``ack`` beyond the ``state`` frames sent changes nothing."""
    registry = registries()
    fake, tab, space = await acknowledging(registry)
    for _ in range(5):
        tab.ack()
    space.hub.broadcast({"type": "state", "n": 0})
    await wait_for(lambda: any(f.get("n") == 0 for f in fake.frames))
    for n in range(1, 3):
        space.hub.broadcast({"type": "state", "n": n})
    await settle(0.1)
    assert [f.get("n") for f in marked(fake)] == [0]


async def test_a_tab_that_never_acknowledges_is_not_held_back(registries):
    """§4.3 "Acknowledgement": a tab that never sent ``ack`` is sent frames as they come."""
    registry = registries()
    fake = FakeTab()
    await registry.attach(owner(), fake.send, fake.close)
    space = registry.live["owner"]
    await wait_for(lambda: len(fake.texts) >= 2)
    for n in range(3):
        space.hub.broadcast({"type": "state", "n": n})
        await wait_for(lambda n=n: any(f.get("n") == n for f in fake.frames))
    assert [f.get("n") for f in marked(fake)] == [0, 1, 2]


async def test_a_coalesced_analysis_never_arrives_before_its_state(registries):
    """The newest ``state`` and ``analysis`` keep their broadcast order (§4.3): the page ignores
    an ``analysis`` whose cursor its ``state`` has not reached (§3.8)."""
    registry = registries(queue_size=8)
    tab, space = await stalled(registry)
    space.hub.broadcast({"type": "analysis", "cursor": 0, "n": 0})
    for n in range(1, 50):
        space.hub.broadcast({"type": "state", "n": n})
        space.hub.broadcast({"type": "analysis", "cursor": n, "n": n})
    unstick(tab)
    await wait_for(lambda: any(f.get("type") == "analysis" and f.get("n") == 49
                               for f in tab.frames))
    assert [(f["type"], f["n"]) for f in marked(tab)] == [("state", 49), ("analysis", 49)]


async def test_other_frames_still_overflow_a_stalled_tab_with_coalesced_frames_queued(registries):
    """Frames that are neither coalesced nor folded keep the queue bounded: ``error`` frames
    still close with 1013 (§4.3 Overflow). ``log`` frames no longer can: they fold."""
    registry = registries(queue_size=8)
    tab, space = await stalled(registry)
    for n in range(20):
        space.hub.broadcast({"type": "state", "n": n})
        space.hub.broadcast({"type": "analysis", "cursor": n, "n": n})
    await settle(0.05)
    assert tab.closed == []
    for n in range(8):
        space.hub.broadcast(error(f"t{n}"))
    await wait_for(lambda: tab.closed)
    assert tab.closed == [1013]


async def test_a_stalled_tab_is_not_closed_by_log_frames_they_fold_into_one_history(registries):
    """§4.3 Log folding: a ``log`` that would overflow the queue replaces every queued ``log``
    (and ``log_history``) with one ``log_history`` of the current history at the end, so a tab
    that does not read keeps its socket, and what it gets still ends in the newest lines."""
    registry = registries(queue_size=8)
    tab, space = await stalled(registry)
    queue = space.hub.tabs[0].queue
    histories = []
    for n in range(500):
        space.session._record_log("note", f"t{n}")
        histories.append(sum(1 for kind, _ in queue._items if kind == "log_history"))
    await settle(0.1)
    assert (tab.closed, max(histories)) == ([], 1)
    kinds = [kind for kind, _ in queue._items]
    assert kinds.count("log_history") == 1 and len(kinds) <= 8
    unstick(tab)
    await wait_for(lambda: any(f.get("type") == "log" and f["line"]["text"] == "t499"
                               or f.get("type") == "log_history"
                               and f["lines"] and f["lines"][-1]["text"] == "t499"
                               for f in tab.frames))
    frames = tab.frames
    assert [f.get("type") for f in frames].count("log_history") == 1
    start = [f.get("type") for f in frames].index("log_history")
    seen = [line["text"] for line in frames[start]["lines"]]
    seen += [f["line"]["text"] for f in frames[start + 1:] if f.get("type") == "log"]
    seen = [text for text in seen if text.startswith("t")]
    assert len(seen) >= 100
    assert seen == [f"t{i}" for i in range(500 - len(seen), 500)]
    assert tab.closed == []


@pytest.mark.parametrize("kind", ["state", "analysis", "log", "log_history"])
async def test_a_queue_full_of_log_frames_folds_for_any_foldable_frame(kind):
    """§4.3 Log folding: a ``state``, ``analysis`` or ``log_history`` with nothing of its type to
    supersede folds the queued logs first, like a ``log`` does; it is not refused."""
    from gowui.spaces import TabQueue

    queue = TabQueue(256)
    history = lambda: json.dumps({"type": "log_history", "lines": []})  # noqa: E731
    assert all(queue.put("log", json.dumps(note(str(n))), history) for n in range(256))
    assert queue.put(kind, json.dumps({"type": kind}), history) is True
    kinds = [queued for queued, _ in queue._items]
    assert kinds.count("log_history") == 1 and "log" not in kinds


def test_a_queue_mostly_of_errors_is_refused_rather_than_folded_on_every_log_line():
    """§4.3 Overflow: once a fold would leave more than half the queue unfoldable, the tab is
    closed; folding for it on every log line would tie up the event loop."""
    from gowui.spaces import TabQueue

    calls = []

    def history() -> str:
        calls.append(1)
        return json.dumps({"type": "log_history", "lines": []})

    queue = TabQueue(8)
    for n in range(7):
        assert queue.put("error", json.dumps(error(str(n))), history)
    accepted = [queue.put("log", json.dumps(note(str(n))), history) for n in range(200)]
    assert False in accepted or len(calls) <= 10, (accepted.count(False), len(calls))


async def test_folding_in_many_tabs_encodes_the_history_once_per_log_line(registries,
                                                                          monkeypatch):
    """§4.3 Log folding: the history text is encoded once and shared by every tab until the log
    changes, so many stalled tabs cost one encoding per log line, not one per tab."""
    registry = registries(queue_size=8)
    tabs = []
    for _ in range(20):
        tab = FakeTab(stuck=True)
        await registry.attach(owner(), tab.send, tab.close)
        tabs.append(tab)
    await settle(0.05)
    space = registry.live["owner"]
    session = space.session
    built = []
    original = session._log_history

    def counting():
        built.append(1)
        return original()

    monkeypatch.setattr(session, "_log_history", counting)
    for n in range(200):
        session._record_log("note", f"t{n}")
    assert [tab.closed for tab in tabs] == [[]] * 20
    assert 0 < len(built) <= 200, len(built)


async def test_the_other_tabs_keep_receiving_after_one_overflows(registries):
    registry = registries(queue_size=4)
    stuck, reading = FakeTab(stuck=True), FakeTab()
    await registry.attach(owner(), stuck.send, stuck.close)
    await registry.attach(owner(), reading.send, reading.close)
    space = registry.live["owner"]
    for index in range(20):
        space.hub.broadcast(error(str(index)))  # errors: logs would fold, not overflow (§4.3)
        await asyncio.sleep(0.01)  # the reading tab's sender drains as it goes
    await wait_for(lambda: stuck.closed)
    space.hub.broadcast({"type": "log", "line": {"direction": "note", "text": "after", "at": 0}})
    got = await wait_for(lambda: any(f.get("type") == "log" and f["line"]["text"] == "after"
                                     for f in reading.frames))
    assert (stuck.closed, got, reading.closed) == ([1013], True, [])


async def test_an_overflowed_tab_is_detached_and_gets_nothing_more(registries):
    registry = registries(queue_size=4)
    stuck = FakeTab(stuck=True)
    await registry.attach(owner(), stuck.send, stuck.close)
    space = registry.live["owner"]
    for index in range(20):
        space.hub.broadcast(error(str(index)))  # errors: logs would fold, not overflow (§4.3)
    await wait_for(lambda: stuck.closed)
    stuck.stuck = False
    stuck._never.set()
    for index in range(20):
        space.hub.broadcast({"type": "log", "line": {"direction": "note", "text": "later",
                                                     "at": 0}})
    await settle(0.2)
    assert [f for f in stuck.frames if f.get("type") == "log"
            and f["line"]["text"] == "later"] == []


# -- saving (§8.2) -------------------------------------------------------------------------------
async def test_a_fresh_space_is_not_written_until_it_changes(registries):
    storage = Storage()
    registry = registries(storage)
    await registry.get(owner())
    await registry.save_changed()
    assert storage.saves == []


async def test_a_restored_space_is_not_rewritten_until_it_changes(registries):
    storage = Storage({"owner": snapshot_with("D4")})
    registry = registries(storage)
    await registry.get(owner())
    await registry.save_changed()
    assert storage.saves == []


async def test_a_changed_space_is_saved_once(registries):
    storage = Storage()
    registry = registries(storage)
    await play(await registry.get(owner()))
    await registry.save_changed()
    await registry.save_changed()
    assert len(storage.saves) == 1


async def test_a_save_stores_the_session_snapshot(registries):
    storage = Storage()
    registry = registries(storage)
    space = await registry.get(owner())
    await play(space)
    await registry.save_changed()
    assert storage.stored() == space.session.snapshot()


async def test_saves_run_in_a_worker_thread(registries):
    storage = Storage()
    registry = registries(storage)
    await play(await registry.get(owner()))
    await registry.save_changed()
    saves = [thread for kind, thread in storage.threads if kind == "save"]
    assert saves and threading.main_thread() not in saves


async def test_the_periodic_task_saves_a_changed_space(registries):
    storage = Storage()
    registry = registries(storage, save_interval=0.05)
    await registry.start()
    await play(await registry.get(owner()))
    assert await wait_for(lambda: len(storage.saves) == 1)


async def test_the_periodic_task_does_not_rewrite_an_unchanged_space(registries):
    storage = Storage()
    registry = registries(storage, save_interval=0.05)
    await registry.start()
    await play(await registry.get(owner()))
    await wait_for(lambda: len(storage.saves) == 1)
    await settle(0.3)
    assert len(storage.saves) == 1


async def test_the_last_tab_to_detach_saves_the_space(registries):
    storage = Storage()
    registry = registries(storage)
    first, second = FakeTab(), FakeTab()
    tab1 = await registry.attach(owner(), first.send, first.close)
    tab2 = await registry.attach(owner(), second.send, second.close)
    await play(registry.live["owner"])
    await registry.detach(tab1)
    saved_after_first = len(storage.saves)
    await registry.detach(tab2)
    assert (saved_after_first, len(storage.saves)) == (0, 1)


async def test_an_older_snapshot_never_overwrites_a_newer_one(registries):
    storage = Storage()
    registry = registries(storage)
    space = await registry.get(owner())
    await play(space, "D4")
    storage.gate.clear()
    storage.saving.clear()
    older = asyncio.create_task(registry.save_changed())
    await asyncio.to_thread(storage.saving.wait, HANG)
    await play(space, "E5")
    newer = asyncio.create_task(registry.save_changed())
    await settle(0.1)
    started_while_gated = [kind for kind, _ in storage.threads].count("save")
    storage.gate.set()
    await asyncio.wait_for(asyncio.gather(older, newer), HANG)
    assert (started_while_gated, storage.stored()["boards"][0]["cursor"]) == (1, 2)


# -- idle release (§7.8, §8.2) --------------------------------------------------------------------
async def test_a_space_without_idle_release_is_never_released(registries):
    clock = Clock()
    registry = registries(clock=clock)
    tab = FakeTab()
    attached = await registry.attach(owner(), tab.send, tab.close)
    await registry.detach(attached)
    clock.now += 10 ** 9
    await registry.release_idle()
    assert list(registry.live) == ["owner"]


async def test_a_space_is_kept_until_the_idle_time_has_passed(registries):
    clock = Clock()
    registry = registries(idle=60.0, clock=clock)
    tab = FakeTab()
    await registry.detach(await registry.attach(owner(), tab.send, tab.close))
    clock.now += 59
    await registry.release_idle()
    assert list(registry.live) == ["owner"]


async def test_an_idle_space_is_released(registries):
    clock = Clock()
    registry = registries(idle=60.0, clock=clock)
    tab = FakeTab()
    await registry.detach(await registry.attach(owner(), tab.send, tab.close))
    clock.now += 61
    await registry.release_idle()
    assert list(registry.live) == []


async def test_a_released_space_is_saved_and_closed(registries, monkeypatch):
    from gowui.session import GameSession

    closed = []
    real = GameSession.aclose

    async def aclose(self):
        closed.append(self)
        await real(self)

    monkeypatch.setattr(GameSession, "aclose", aclose)
    clock = Clock()
    storage = Storage()
    registry = registries(storage, idle=60.0, clock=clock)
    tab = FakeTab()
    attached = await registry.attach(owner(), tab.send, tab.close)
    space = attached.space
    await play(space)
    await registry.detach(attached)
    storage.saves.clear()
    await play(space, "E5")
    clock.now += 61
    await registry.release_idle()
    assert (len(storage.saves), closed) == (1, [space.session])


async def test_a_released_space_comes_back_intact_on_next_use(registries):
    clock = Clock()
    registry = registries(Storage(), idle=60.0, clock=clock)
    tab = FakeTab()
    attached = await registry.attach(owner(), tab.send, tab.close)
    await play(attached.space, "D4")
    await play(attached.space, "E5")
    await registry.detach(attached)
    clock.now += 61
    await registry.release_idle()
    again = await registry.get(owner())
    assert (again is not attached.space,
            again.session.state_message()["game"]["moveCount"]) == (True, 2)


async def test_a_space_with_a_tab_attached_is_not_released(registries):
    clock = Clock()
    registry = registries(idle=60.0, clock=clock)
    first, second = FakeTab(), FakeTab()
    kept = await registry.attach(owner(), first.send, first.close)
    await registry.detach(await registry.attach(owner(), second.send, second.close))
    clock.now += 61
    await registry.release_idle()
    assert registry.live.get("owner") is kept.space


async def test_a_tab_attaching_during_a_release_gets_one_space_with_the_newest_snapshot(
        registries):
    clock = Clock()
    storage = Storage()
    registry = registries(storage, idle=60.0, clock=clock)
    first = FakeTab()
    attached = await registry.attach(owner(), first.send, first.close)
    old = attached.space
    await play(old, "D4")
    await play(old, "E5")
    await registry.detach(attached)
    storage.saves.clear()
    await play(old, "F6")  # changed since the detach save: the release must save it
    clock.now += 61
    storage.gate.clear()
    storage.saving.clear()
    release = asyncio.create_task(registry.release_idle())
    await asyncio.to_thread(storage.saving.wait, HANG)
    second = FakeTab()
    attaching = asyncio.create_task(registry.attach(owner(), second.send, second.close))
    await settle(0.1)
    waited = not attaching.done()
    storage.gate.set()
    await asyncio.wait_for(release, HANG)
    new = (await asyncio.wait_for(attaching, HANG)).space
    assert (waited, new is not old, list(registry.live), registry.live["owner"] is new,
            new.session.state_message()["game"]["moveCount"],
            storage.stored()["boards"][0]["cursor"]) == (True, True, ["owner"], True, 3, 3)


# -- shutdown (§8.2) -------------------------------------------------------------------------------
async def test_shutdown_saves_every_changed_space(registries):
    storage = Storage()
    registry = registries(storage)
    await play(await registry.get(identity("alice")))
    await play(await registry.get(identity("bob")))
    await registry.aclose()
    assert sorted(key for key, _ in storage.saves) == ["alice", "bob"]


async def test_shutdown_closes_every_session(registries, monkeypatch):
    from gowui.session import GameSession

    closed = []
    real = GameSession.aclose

    async def aclose(self):
        closed.append(self)
        await real(self)

    monkeypatch.setattr(GameSession, "aclose", aclose)
    registry = registries()
    spaces = [await registry.get(identity("alice")), await registry.get(identity("bob"))]
    await registry.aclose()
    assert sorted(id(s) for s in closed) == sorted(id(s.session) for s in spaces)


async def test_shutdown_closes_the_engine_connection(registries, gtp_server):
    registry = registries(Storage({"owner": snapshot_with("D4", connected=True, request={
        "protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})}))
    space = await registry.get(owner())
    await wait_for(lambda: space.session.state_message()["engine"]["connected"])
    await registry.aclose()
    assert await wait_for(lambda: gtp_server.open_connections == 0)
