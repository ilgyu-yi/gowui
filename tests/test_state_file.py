"""The local state file (SPEC §6.4, §8.2, §8.3, §9): the default path per platform, the read cap,
setting a bad file aside, atomic private writes, ASCII-escaped JSON, ``--fresh`` in memory only,
and a restart that brings the same boards back.
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
import sys
from pathlib import Path

import pytest

from app_helpers import local_bundle, snapshot_with
from session_helpers import settle

BAD_NAME = re.compile(r"state\.json\.bad-\d{8}T\d{6}Z\S*")
POSIX_MODES = pytest.mark.skipif(sys.platform == "win32", reason="file modes are not applied")


def storage(path: Path, **kwargs):
    from gowui.local_mode import JsonFileStorage

    return JsonFileStorage(path, **kwargs)


def set_aside_files(directory: Path) -> list[Path]:
    return sorted(p for p in directory.iterdir() if BAD_NAME.fullmatch(p.name))


# -- default path (§8.3) ---------------------------------------------------------------------------
HOME = Path("/home/someone")


def default_path(platform: str, env: dict) -> Path:
    from gowui.local_mode import default_state_path

    return default_state_path(platform, env, HOME)


def test_the_default_path_on_macos_is_under_application_support():
    assert default_path("darwin", {}) == HOME / "Library" / "Application Support" / "gowui" / \
        "state.json"


def test_the_default_path_on_windows_is_under_localappdata():
    assert default_path("win32", {"LOCALAPPDATA": "/data/local"}) == \
        Path("/data/local") / "gowui" / "state.json"


@pytest.mark.parametrize("env", [{}, {"LOCALAPPDATA": ""}])
def test_the_default_path_on_windows_without_localappdata_is_under_the_home(env):
    assert default_path("win32", env) == HOME / "AppData" / "Local" / "gowui" / "state.json"


def test_the_default_path_elsewhere_is_under_xdg_state_home():
    assert default_path("linux", {"XDG_STATE_HOME": "/xdg/state"}) == \
        Path("/xdg/state") / "gowui" / "state.json"


@pytest.mark.parametrize("env", [{}, {"XDG_STATE_HOME": ""}, {"XDG_STATE_HOME": "relative/state"},
                                 {"XDG_STATE_HOME": "./state"}])
def test_the_default_path_elsewhere_ignores_an_unset_empty_or_relative_xdg_state_home(env):
    assert default_path("linux", env) == HOME / ".local" / "state" / "gowui" / "state.json"


def test_a_bsd_platform_uses_the_xdg_rule():
    assert default_path("freebsd14", {}) == HOME / ".local" / "state" / "gowui" / "state.json"


# -- reading (§8.3) -------------------------------------------------------------------------------
def test_the_read_cap_is_64_boards_of_six_times_one_mib_plus_one_mib():
    from gowui.local_mode import STATE_READ_CAP

    assert STATE_READ_CAP == 64 * (1024 * 1024) * 6 + 1024 * 1024 == 403_701_760


def test_a_file_storage_reads_at_most_the_cap_by_default(tmp_path):
    from gowui.local_mode import STATE_READ_CAP

    assert storage(tmp_path / "state.json").max_bytes == STATE_READ_CAP


def test_a_missing_file_loads_as_nothing_stored(tmp_path):
    assert storage(tmp_path / "state.json").load("owner") is None


def test_loading_a_missing_file_creates_nothing(tmp_path):
    storage(tmp_path / "state" / "state.json").load("owner")
    assert list(tmp_path.iterdir()) == []


def test_a_saved_snapshot_loads_back_equal(tmp_path):
    store = storage(tmp_path / "state.json")
    store.save("owner", snapshot_with("D4", "E5"))
    assert store.load("owner") == snapshot_with("D4", "E5")


def test_a_file_exactly_at_the_cap_is_read(tmp_path):
    path = tmp_path / "state.json"
    text = json.dumps(snapshot_with("D4"))
    path.write_text(text)
    assert storage(path, max_bytes=len(text)).load("owner") == snapshot_with("D4")


BAD_CONTENT = {
    "not utf-8": b"\xff\xfe{\x00",
    "empty": b"",
    "not json": b"this is not json",
    "truncated json": b'{"version": 1, "boards": [',
    "a json array": b"[1, 2, 3]",
    "a json string": b'"text"',
    "json null": b"null",
    "nested too deeply": b"[" * 200_000 + b"]" * 200_000,
}


@pytest.mark.parametrize("name", sorted(BAD_CONTENT))
def test_a_bad_file_loads_as_nothing_stored(tmp_path, name):
    path = tmp_path / "state.json"
    path.write_bytes(BAD_CONTENT[name])
    assert storage(path).load("owner") is None


@pytest.mark.parametrize("name", sorted(BAD_CONTENT))
def test_a_bad_file_is_renamed_with_a_utc_timestamp(tmp_path, name):
    path = tmp_path / "state.json"
    path.write_bytes(BAD_CONTENT[name])
    storage(path).load("owner")
    assert (path.exists(), len(set_aside_files(tmp_path))) == (False, 1)


def test_a_set_aside_file_keeps_its_content(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b"this is not json")
    storage(path).load("owner")
    assert set_aside_files(tmp_path)[0].read_bytes() == b"this is not json"


def test_setting_a_file_aside_logs_a_warning_naming_the_new_path(tmp_path, caplog):
    path = tmp_path / "state.json"
    path.write_bytes(b"this is not json")
    with caplog.at_level(logging.WARNING):
        storage(path).load("owner")
    new_path = str(set_aside_files(tmp_path)[0])
    assert any(r.levelno >= logging.WARNING and new_path in r.getMessage()
               for r in caplog.records)


def test_a_file_over_the_cap_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    text = json.dumps(snapshot_with("D4"))
    path.write_text(text)
    loaded = storage(path, max_bytes=len(text) - 1).load("owner")
    assert (loaded, len(set_aside_files(tmp_path))) == (None, 1)


@POSIX_MODES
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads anything")
def test_a_file_that_cannot_be_read_is_set_aside(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(snapshot_with("D4")))
    path.chmod(0)
    loaded = storage(path).load("owner")
    assert (loaded, len(set_aside_files(tmp_path))) == (None, 1)


def test_set_aside_moves_a_stored_file_out_of_the_way(tmp_path):
    path = tmp_path / "state.json"
    path.write_text(json.dumps(snapshot_with("D4")))
    storage(path).set_aside("owner", "refused by restore")
    assert (path.exists(), len(set_aside_files(tmp_path))) == (False, 1)


def test_two_files_set_aside_in_the_same_second_are_both_kept(tmp_path):
    """Nothing is deleted (§8.3), even when two set-asides share a timestamp."""
    path = tmp_path / "state.json"
    store = storage(path)
    path.write_bytes(b"first")
    store.load("owner")
    path.write_bytes(b"second")
    store.load("owner")
    assert sorted(p.read_bytes() for p in set_aside_files(tmp_path)) == [b"first", b"second"]


# -- writing (§8.3) -------------------------------------------------------------------------------
@POSIX_MODES
def test_the_state_file_is_written_with_mode_0600(tmp_path):
    path = tmp_path / "state.json"
    storage(path).save("owner", snapshot_with("D4"))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX_MODES
def test_a_missing_parent_directory_is_created_with_mode_0700(tmp_path):
    path = tmp_path / "gowui" / "state.json"
    storage(path).save("owner", snapshot_with("D4"))
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@POSIX_MODES
def test_an_existing_directory_keeps_its_mode(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir()
    directory.chmod(0o755)
    storage(directory / "state.json").save("owner", snapshot_with("D4"))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o755


def test_a_save_leaves_no_temporary_file(tmp_path):
    path = tmp_path / "state.json"
    store = storage(path)
    store.save("owner", snapshot_with("D4"))
    store.save("owner", snapshot_with("D4", "E5"))
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_a_save_renames_a_temporary_file_from_the_same_directory_over_the_file(tmp_path,
                                                                             monkeypatch):
    path = tmp_path / "state.json"
    calls = []
    real = os.replace

    def replace(src, dst, *args, **kwargs):
        calls.append((Path(src).parent, Path(dst)))
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", replace)
    storage(path).save("owner", snapshot_with("D4"))
    assert calls == [(tmp_path, path)]


def test_a_save_is_flushed_with_fsync(tmp_path, monkeypatch):
    calls = []
    real = os.fsync

    def fsync(fd):
        calls.append(fd)
        return real(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    storage(tmp_path / "state.json").save("owner", snapshot_with("D4"))
    assert calls


def test_a_save_replaces_the_previous_content(tmp_path):
    path = tmp_path / "state.json"
    store = storage(path)
    store.save("owner", snapshot_with("D4"))
    store.save("owner", snapshot_with("D4", "E5"))
    assert json.loads(path.read_text()) == snapshot_with("D4", "E5")


def test_the_state_file_is_ascii_escaped_json(tmp_path):
    path = tmp_path / "state.json"
    storage(path).save("owner", snapshot_with("D4", name="바둑 \ud800 é"))
    assert path.read_bytes().isascii()


def test_a_lone_surrogate_in_a_board_name_is_restored_unchanged(tmp_path):
    store = storage(tmp_path / "state.json")
    store.save("owner", snapshot_with("D4", name="a\ud800b"))
    assert store.load("owner")["boards"][0]["name"] == "a\ud800b"


# -- the memory storage (§6.4) ---------------------------------------------------------------------
def test_the_memory_storage_starts_empty():
    from gowui.local_mode import MemoryStorage

    assert MemoryStorage().load("owner") is None


def test_the_memory_storage_keeps_what_was_saved():
    from gowui.local_mode import MemoryStorage

    store = MemoryStorage()
    store.save("owner", snapshot_with("D4"))
    assert store.load("owner") == snapshot_with("D4")


# -- through the registry: a snapshot restore refuses is set aside (§3.1, §8.1) -------------------
async def test_a_version_2_file_is_set_aside_and_the_space_starts_fresh(tmp_path):
    from gowui.policies import Identity
    from gowui.spaces import SpaceRegistry

    path = tmp_path / "state.json"
    path.write_text(json.dumps({**snapshot_with("D4", "E5"), "version": 2}))
    registry = SpaceRegistry(local_bundle(storage(path)))
    try:
        space = await registry.get(Identity(key="owner", name="", source="none",
                                            logout_kind="", logout_url=""))
        moves = space.session.state_message()["game"]["moveCount"]
    finally:
        await registry.aclose()
    assert (moves, path.exists(), len(set_aside_files(tmp_path))) == (0, False, 1)


# -- the app: restart, --state, --fresh (§8.2, §8.3, §9) --------------------------------------------
async def ready_tab(tabs, running):
    tab = await tabs(running)
    assert not isinstance(tab, int), f"the handshake was refused with HTTP {tab}"
    assert await tab.wait_state() is not None, "no state frame on attach"
    return tab


async def send_and_wait(tab, message: dict, predicate) -> dict:
    start = tab.mark()
    await tab.send(message)
    frame = await tab.wait_state(predicate, start)
    assert frame is not None, f"{message} had no effect: {tab.of('error', start)}"
    return frame


async def configure(tab) -> dict:
    """Two boards with moves, names and a past cursor; the second board active."""
    await send_and_wait(tab, {"type": "load_sgf", "sgf": snapshot_with("D4", "E5", "F6")[
        "boards"][0]["sgf"]}, lambda f: f["game"]["moveCount"] == 3)
    first = tab.state()["activeBoard"]
    await send_and_wait(tab, {"type": "board_rename", "id": first, "name": "study"},
                        lambda f: any(b["name"] == "study" for b in f["boards"]))
    await send_and_wait(tab, {"type": "navigate", "index": 1},
                        lambda f: f["game"]["cursor"] == 1)
    await send_and_wait(tab, {"type": "board_duplicate"}, lambda f: len(f["boards"]) == 2)
    second = tab.state()["activeBoard"]
    await send_and_wait(tab, {"type": "board_rename", "id": second, "name": "copy"},
                        lambda f: any(b["name"] == "copy" for b in f["boards"]))
    return tab.state()


def summary(state: dict) -> tuple:
    return (state["activeBoard"],
            [(b["id"], b["name"], b["moveCount"], b["cursor"]) for b in state["boards"]])


async def test_a_restart_on_the_same_state_file_brings_back_boards_names_and_cursors(
        serve, tabs, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    first = await serve(build_app(parse(["--state", str(path)])))
    before = summary(await configure(await ready_tab(tabs, first)))
    await first.stop()
    second = await serve(build_app(parse(["--state", str(path)])))
    after = summary((await ready_tab(tabs, second)).state())
    assert after == before


async def test_a_restart_keeps_a_lone_surrogate_board_name(serve, tabs, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    first = await serve(build_app(parse(["--state", str(path)])))
    tab = await ready_tab(tabs, first)
    board = tab.state()["activeBoard"]
    await tab.send(json.dumps({"type": "board_rename", "id": board, "name": "a\ud800b"}))
    await tab.wait_state(lambda f: any(b["name"] != "Board 1" for b in f["boards"]))
    await first.stop()
    second = await serve(build_app(parse(["--state", str(path)])))
    state = (await ready_tab(tabs, second)).state()
    assert state["boards"][0]["name"] == "a\ud800b"


async def test_shutdown_saves_the_state_file(serve, tabs, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    running = await serve(build_app(parse(["--state", str(path)])))
    tab = await ready_tab(tabs, running)
    await send_and_wait(tab, {"type": "load_sgf", "sgf": snapshot_with("D4")["boards"][0]["sgf"]},
                        lambda f: f["game"]["moveCount"] == 1)
    await running.stop()
    assert json.loads(path.read_text())["boards"][0]["cursor"] == 1


async def test_a_fresh_start_writes_nothing_until_something_changes(serve, tabs, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    running = await serve(build_app(parse(["--state", str(path)])))
    await ready_tab(tabs, running)
    await running.stop()
    assert path.exists() is False


async def test_a_bad_state_file_is_set_aside_at_startup(serve, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    path.write_bytes(b"this is not json")
    await serve(build_app(parse(["--state", str(path)])))
    assert (path.exists(), len(set_aside_files(tmp_path))) == (False, 1)


@pytest.fixture
def private_home(tmp_path, monkeypatch):
    """HOME and the platform's state variables pointed into ``tmp_path``; returns the default
    state path the CLI will use."""
    from gowui.local_mode import default_state_path

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "localappdata"))
    return default_state_path(sys.platform, os.environ, Path.home())


async def test_without_state_flags_the_default_path_is_used(serve, tabs, private_home):
    from gowui.cli import build_app, parse

    running = await serve(build_app(parse([])))
    tab = await ready_tab(tabs, running)
    await send_and_wait(tab, {"type": "load_sgf", "sgf": snapshot_with("D4")["boards"][0]["sgf"]},
                        lambda f: f["game"]["moveCount"] == 1)
    await running.stop()
    assert private_home.exists()


async def test_fresh_starts_empty_even_when_the_default_file_has_boards(serve, tabs,
                                                                         private_home):
    from gowui.cli import build_app, parse

    private_home.parent.mkdir(parents=True)
    private_home.write_text(json.dumps(snapshot_with("D4", "E5", name="kept")))
    running = await serve(build_app(parse(["--fresh"])))
    state = (await ready_tab(tabs, running)).state()
    assert [(b["name"], b["moveCount"]) for b in state["boards"]] == [("Board 1", 0)]


async def test_fresh_leaves_the_default_file_untouched_after_changes_and_shutdown(
        serve, tabs, private_home):
    from gowui.cli import build_app, parse

    private_home.parent.mkdir(parents=True)
    private_home.write_text(json.dumps(snapshot_with("D4", "E5", name="kept")))
    os.utime(private_home, (1_600_000_000, 1_600_000_000))
    before = (private_home.read_bytes(), private_home.stat().st_mtime_ns,
              sorted(p.name for p in private_home.parent.iterdir()))
    running = await serve(build_app(parse(["--fresh"])))
    tab = await ready_tab(tabs, running)
    await send_and_wait(tab, {"type": "load_sgf",
                              "sgf": snapshot_with("Q16", size=19)["boards"][0]["sgf"]},
                        lambda f: f["game"]["moveCount"] == 1)
    await running.stop()
    after = (private_home.read_bytes(), private_home.stat().st_mtime_ns,
             sorted(p.name for p in private_home.parent.iterdir()))
    assert after == before


async def test_fresh_creates_no_file_or_directory(serve, tabs, private_home):
    from gowui.cli import build_app, parse

    home = Path(os.environ["HOME"])
    running = await serve(build_app(parse(["--fresh"])))
    tab = await ready_tab(tabs, running)
    await send_and_wait(tab, {"type": "load_sgf", "sgf": snapshot_with("D4")["boards"][0]["sgf"]},
                        lambda f: f["game"]["moveCount"] == 1)
    await running.stop()
    assert list(home.iterdir()) == []


async def test_fresh_does_not_set_aside_a_bad_default_file(serve, private_home):
    from gowui.cli import build_app, parse

    private_home.parent.mkdir(parents=True)
    private_home.write_bytes(b"this is not json")
    running = await serve(build_app(parse(["--fresh"])))
    await settle(0.2)
    await running.stop()
    assert private_home.read_bytes() == b"this is not json"


@POSIX_MODES
async def test_the_saved_state_file_is_private(serve, tabs, tmp_path):
    from gowui.cli import build_app, parse

    path = tmp_path / "state.json"
    running = await serve(build_app(parse(["--state", str(path)])))
    tab = await ready_tab(tabs, running)
    await send_and_wait(tab, {"type": "load_sgf", "sgf": snapshot_with("D4")["boards"][0]["sgf"]},
                        lambda f: f["game"]["moveCount"] == 1)
    await running.stop()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_local_bundle_passes_the_storage_through(tmp_path):
    store = storage(tmp_path / "state.json")
    assert local_bundle(store).storage is store
