"""The space registry's server-mode guards (SPEC §3.1 step 3, §8.2; carry-over F5 and F11 of #9).

- A release whose save fails keeps the space live, so the next attach never reloads an older
  snapshot (§8.2 "A failed save stops the release") — for the snapshot, and for the preferences
  that ride the same pass (§6.4).
- A space whose creation was in flight when shutdown took its list is closed and never published
  (§3.1 step 3).
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading

from app_helpers import local_bundle
from helpers import HANG


def alice():
    from gowui.policies import Identity

    return Identity(key="local:a1", name="alice", source="local", logout_kind="local")


class FailingStorage:
    def __init__(self) -> None:
        self.fail = True
        self.saved: dict | None = None

    def load(self, key):
        return None

    def save(self, key, snapshot):
        if self.fail:
            raise OSError("disk full")
        self.saved = snapshot

    def set_aside(self, key, reason):
        pass


async def _noop(*_):
    return None


async def test_a_release_whose_save_fails_keeps_the_space_live():
    from gowui.spaces import SpaceRegistry

    storage = FailingStorage()
    registry = SpaceRegistry(dataclasses.replace(local_bundle(storage), idle_release_seconds=0.0))
    try:
        tab = await registry.attach(alice(), _noop, _noop)
        space = tab.space
        await space.session.handle({"type": "play", "color": "black", "vertex": "D4"})
        await registry.detach(tab)
        await registry.release_idle()
        assert registry.live.get("local:a1") is space

        storage.fail = False
        await registry.release_idle()
        assert "local:a1" not in registry.live
        assert storage.saved is not None
    finally:
        await registry.aclose()


class FailingPreferences(FailingStorage):
    """A storage that keeps preferences (§6.4) and cannot write them."""

    keeps_preferences = True

    def __init__(self) -> None:
        super().__init__()
        self.fail = False          # the snapshot writes; only the preferences do not
        self.fail_preferences = True
        self.preferences: dict | None = None

    def load_preferences(self, key):
        return None

    def save_preferences(self, key, preferences):
        if self.fail_preferences:
            raise OSError("disk full")
        self.preferences = preferences


async def test_a_release_whose_preferences_save_fails_keeps_the_space_live():
    """§8.2: the preferences ride the same pass, so a failed write blocks the release too."""
    from gowui.spaces import SpaceRegistry

    storage = FailingPreferences()
    registry = SpaceRegistry(dataclasses.replace(local_bundle(storage), idle_release_seconds=0.0))
    try:
        tab = await registry.attach(alice(), _noop, _noop)
        space = tab.space
        await space.session.handle({"type": "preferences", "lang": "ko"})
        await registry.detach(tab)
        await registry.release_idle()
        assert registry.live.get("local:a1") is space
        assert storage.preferences is None

        storage.fail_preferences = False
        await registry.release_idle()
        assert "local:a1" not in registry.live
        assert storage.preferences == {"lang": "ko", "presets": []}
    finally:
        await registry.aclose()


class BlockingStorage:
    def __init__(self) -> None:
        self.loading = threading.Event()
        self.release = threading.Event()

    def load(self, key):
        self.loading.set()
        assert self.release.wait(HANG)
        return None

    def save(self, key, snapshot):
        pass

    def set_aside(self, key, reason):
        pass


async def test_a_space_created_during_shutdown_is_never_published(monkeypatch):
    from gowui.session import GameSession
    from gowui.spaces import SpaceRegistry

    closed = []
    real = GameSession.aclose

    async def aclose(self):
        closed.append(self)
        await real(self)

    monkeypatch.setattr(GameSession, "aclose", aclose)
    storage = BlockingStorage()
    registry = SpaceRegistry(local_bundle(storage))
    getting = asyncio.create_task(registry.get(alice()))
    await asyncio.to_thread(storage.loading.wait, HANG)
    await registry.aclose()
    storage.release.set()
    result = await asyncio.gather(getting, return_exceptions=True)
    assert isinstance(result[0], Exception)
    assert registry.live == {}
    assert len(closed) == 1
