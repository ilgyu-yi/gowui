"""Message handling shared by both modes (SPEC §4.1, §4.2, §7.6, §1.2, §1.4, §1.5): frames that are
not objects, unknown types and wrong-typed fields are errors that change nothing; numbers out of
range are clamped; SGF uploads are bounded and parsed off the event loop; attach frames come in
order; and every frame is valid JSON.
"""

from __future__ import annotations

import threading
import time

import pytest

from gowui.game import Game
from session_helpers import move_list, settle, sgf_of


async def error_after(h, message) -> str | None:
    start = h.rec.mark()
    await h.send(message)
    return await h.rec.wait_error(start)


# -- frames that are not commands -----------------------------------------------------------------
@pytest.mark.parametrize("frame", [[], "play", None, 42, True, [{"type": "state"}]],
                         ids=["list", "string", "null", "number", "bool", "list-of-object"])
async def test_a_frame_that_is_not_an_object_is_an_error(h, frame):
    assert await error_after(h, frame) is not None


@pytest.mark.parametrize("frame", [{}, {"type": "teleport"}, {"type": 5}, {"type": None}],
                         ids=["no-type", "unknown-type", "number-type", "null-type"])
async def test_a_frame_without_a_known_type_is_an_error(h, frame):
    assert await error_after(h, frame) is not None


# -- wrong-typed fields (§4.1) ---------------------------------------------------------------------
WRONG_TYPES = {
    "play-vertex-number": {"type": "play", "color": "black", "vertex": 5},
    "play-color-number": {"type": "play", "color": 7, "vertex": "D4"},
    "play-vertex-off-board": {"type": "play", "color": "black", "vertex": "Z99"},
    "pass-color-number": {"type": "pass", "color": 1},
    "resign-color-unknown": {"type": "resign", "color": "purple"},
    "navigate-index-string": {"type": "navigate", "index": "3"},
    "navigate-index-bool": {"type": "navigate", "index": True},
    "new_game-size-string": {"type": "new_game", "size": "13", "komi": None, "rules": "japanese",
                             "handicap": 0},
    "new_game-size-bool": {"type": "new_game", "size": True, "komi": None, "rules": "japanese",
                           "handicap": 0},
    "new_game-size-range": {"type": "new_game", "size": 26, "komi": None, "rules": "japanese",
                            "handicap": 0},
    "new_game-rules-number": {"type": "new_game", "size": 13, "komi": None, "rules": 5,
                              "handicap": 0},
    "new_game-rules-unknown": {"type": "new_game", "size": 13, "komi": None, "rules": "calvinball",
                               "handicap": 0},
    "new_game-handicap-bool": {"type": "new_game", "size": 13, "komi": None, "rules": "japanese",
                               "handicap": True},
    "new_game-handicap-string": {"type": "new_game", "size": 13, "komi": None,
                                 "rules": "japanese", "handicap": "2"},
    "genmove-color-number": {"type": "genmove", "color": 1},
    "analysis-enabled-string": {"type": "analysis", "enabled": "yes"},
    "analysis-enabled-number": {"type": "analysis", "enabled": 1},
    "players-engine-string": {"type": "players", "blackIsEngine": "true"},
    "players-engine-number": {"type": "players", "whiteIsEngine": 1},
    "players-style-unknown": {"type": "players", "blackStyle": "robot"},
    "engine_params-visits-bool": {"type": "engine_params", "maxVisits": True},
    "engine_params-visits-string": {"type": "engine_params", "maxVisits": "100"},
    "engine_params-interval-string": {"type": "engine_params", "reportInterval": "fast"},
    "engine_params-ownership-string": {"type": "engine_params", "includeOwnership": "yes"},
    "human_params-eval-bool": {"type": "human_params", "evalVisits": True},
    "human_params-eval-string": {"type": "human_params", "evalVisits": "10"},
    "human_params-profile-number": {"type": "human_params", "profile": 5},
    "board_select-id-string": {"type": "board_select", "id": "1"},
    "board_select-id-bool": {"type": "board_select", "id": True},
    "board_rename-name-number": {"type": "board_rename", "id": 1, "name": 5},
    "board_delete-id-null": {"type": "board_delete", "id": None},
    "raw-command-number": {"type": "raw", "command": 5},
    "load_sgf-sgf-number": {"type": "load_sgf", "sgf": 5},
}


@pytest.mark.parametrize("name", sorted(WRONG_TYPES))
async def test_a_wrong_typed_field_is_an_error(h, name):
    assert await error_after(h, WRONG_TYPES[name]) is not None


@pytest.mark.parametrize("name", sorted(WRONG_TYPES))
async def test_a_wrong_typed_field_changes_nothing(h, name):
    await h.play("D4")
    before = h.session.snapshot()
    await h.send(WRONG_TYPES[name])
    await settle(0.1)
    assert h.session.snapshot() == before


BAD_KOMI = {"bool": True, "string": "6.5", "list": [6.5], "overflow": 10 ** 400,
            "infinity": float("inf"), "nan": float("nan")}


@pytest.mark.parametrize("name", sorted(BAD_KOMI))
async def test_a_komi_that_is_not_a_finite_number_gets_a_clear_error(h, name):
    message = await error_after(h, {"type": "new_game", "size": 13, "komi": BAD_KOMI[name],
                                    "rules": "japanese", "handicap": 0})
    assert message is not None and "komi must be a finite number" in message


@pytest.mark.parametrize("name", sorted(BAD_KOMI))
async def test_a_komi_that_is_not_a_finite_number_changes_nothing(h, name):
    before = h.session.snapshot()
    await h.send({"type": "new_game", "size": 13, "komi": BAD_KOMI[name], "rules": "japanese",
                  "handicap": 0})
    await settle(0.1)
    assert h.session.snapshot() == before


async def test_a_disallowed_handicap_is_an_error(h):
    assert await error_after(h, {"type": "new_game", "size": 5, "komi": None,
                                 "rules": "japanese", "handicap": 2}) is not None


# -- clamping (§7.6) -------------------------------------------------------------------------------
@pytest.mark.parametrize("message, key, value", [
    ({"type": "engine_params", "maxVisits": 0}, "maxVisits", 1),
    ({"type": "engine_params", "maxVisits": 2_000_000}, "maxVisits", 1_000_000),
    ({"type": "engine_params", "reportInterval": 0.01}, "reportInterval", 0.1),
    ({"type": "engine_params", "reportInterval": 100}, "reportInterval", 10),
    ({"type": "human_params", "evalVisits": -5}, "evalVisits", 0),
    ({"type": "human_params", "evalVisits": 2_000_000}, "evalVisits", 1_000_000),
], ids=["visits-low", "visits-high", "interval-low", "interval-high", "eval-low", "eval-high"])
async def test_numbers_out_of_range_are_clamped(h, message, key, value):
    await h.send(message)
    assert (await h.fresh_state())["settings"][key] == value


async def test_settings_changes_are_broadcast(h):
    await h.send({"type": "players", "blackIsEngine": True, "whiteStyle": "katago"})
    await h.send({"type": "analysis", "enabled": True})
    await h.send({"type": "engine_params", "maxVisits": 321, "reportInterval": 0.5,
                  "includeOwnership": True})
    settings = (await h.fresh_state())["settings"]
    assert {k: settings[k] for k in ("blackIsEngine", "whiteIsEngine", "whiteStyle",
                                     "analysisEnabled", "maxVisits", "reportInterval",
                                     "includeOwnership")} == \
        {"blackIsEngine": True, "whiteIsEngine": False, "whiteStyle": "katago",
         "analysisEnabled": True, "maxVisits": 321, "reportInterval": 0.5,
         "includeOwnership": True}


# -- game commands ---------------------------------------------------------------------------------
async def test_new_game_applies_size_komi_rules_and_handicap(h):
    await h.send({"type": "new_game", "size": 13, "komi": 5.5, "rules": "chinese",
                  "handicap": 2})
    game = (await h.fresh_state())["game"]
    assert (game["size"], game["komi"], game["rules"], game["handicap"], game["toPlay"]) == \
        (13, 5.5, "chinese", 2, "white")


@pytest.mark.parametrize("rules, handicap, komi", [("chinese", 0, 7.5), ("japanese", 2, 0.5)])
async def test_new_game_without_komi_takes_the_default(h, rules, handicap, komi):
    await h.send({"type": "new_game", "size": 13, "komi": None, "rules": rules,
                  "handicap": handicap})
    assert (await h.fresh_state())["game"]["komi"] == komi


@pytest.mark.parametrize("index, cursor", [(99, 2), (-3, 0)])
async def test_navigate_clamps_the_cursor(h, index, cursor):
    await h.play("D4", "E5")
    await h.send({"type": "navigate", "index": index})
    assert (await h.fresh_state())["game"]["cursor"] == cursor


async def test_undo_takes_back_the_last_move(h):
    await h.play("D4", "E5")
    await h.send({"type": "undo"})
    assert move_list(await h.fresh_state()) == ["D4"]


async def test_a_state_message_gets_a_fresh_state(h):
    await h.play("D4")
    assert (await h.fresh_state())["game"]["lastMove"] == "D4"


# -- SGF (§1.5, §7.6, §4.2) ------------------------------------------------------------------------
async def test_load_sgf_replaces_the_active_game(h):
    await h.send({"type": "load_sgf", "sgf": sgf_of(13, "D4", "K10")})
    state = await h.fresh_state()
    assert (state["game"]["size"], move_list(state)) == (13, ["D4", "K10"])


async def test_an_unreadable_sgf_is_an_error(h):
    assert await error_after(h, {"type": "load_sgf", "sgf": "this is not SGF"}) is not None


async def test_an_unreadable_sgf_changes_nothing(h):
    await h.play("D4")
    before = h.session.snapshot()
    await h.send({"type": "load_sgf", "sgf": "(;SZ[99])"})
    await settle(0.2)
    assert h.session.snapshot() == before


def sgf_of_bytes(total: int, filler: str = "x") -> str:
    """A readable 13x13 SGF of about ``total`` UTF-8 bytes (a long root comment)."""
    head, tail = "(;GM[1]FF[4]SZ[13]C[", "])"
    room = total - len((head + tail).encode())
    width = len(filler.encode())
    return head + filler * (room // width) + tail


OVERSIZE = {"ascii": sgf_of_bytes(1024 * 1024 + 1),
            "utf-8": sgf_of_bytes(1024 * 1024 + 3, "가")}  # 3 bytes, 1 character each


@pytest.mark.parametrize("name", sorted(OVERSIZE))
async def test_an_sgf_over_1_mib_is_refused(h, name):
    """The limit counts UTF-8 bytes: the multi-byte text is under 1 Mi characters."""
    assert await error_after(h, {"type": "load_sgf", "sgf": OVERSIZE[name]}) is not None


@pytest.mark.parametrize("name", sorted(OVERSIZE))
async def test_an_sgf_over_1_mib_changes_nothing(h, name):
    before = h.session.snapshot()
    await h.send({"type": "load_sgf", "sgf": OVERSIZE[name]})
    await settle(0.2)
    assert h.session.snapshot() == before


async def test_an_sgf_of_exactly_1_mib_is_loaded(h):
    text = sgf_of_bytes(1024 * 1024)
    assert len(text.encode()) == 1024 * 1024
    start = h.rec.mark()
    await h.send({"type": "load_sgf", "sgf": text})
    assert await h.rec.wait_state(lambda f: f["game"]["size"] == 13, start)


async def test_load_sgf_is_parsed_off_the_event_loop(h, monkeypatch):
    threads: list[threading.Thread] = []
    real = Game.from_sgf.__func__

    def from_sgf(cls, text):
        threads.append(threading.current_thread())
        return real(cls, text)

    monkeypatch.setattr(Game, "from_sgf", classmethod(from_sgf))
    start = h.rec.mark()
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4")})
    assert await h.rec.wait_state(lambda f: f["game"]["moveCount"] == 1, start)
    assert threads and all(t is not threading.main_thread() for t in threads)


# -- attach frames (§4.2) --------------------------------------------------------------------------
async def test_attach_sends_state_then_log_history(h):
    assert [f["type"] for f in h.session.attach_frames()] == ["state", "log_history"]


async def test_attach_state_is_the_current_state(h):
    await h.play("D4")
    frames = h.session.attach_frames()
    assert frames[0]["game"] == (await h.fresh_state())["game"]


async def test_attach_adds_the_analysis_that_describes_the_position(h, analysis_server):
    await h.connect_to(analysis_server)
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    frames = h.session.attach_frames()
    assert ([f["type"] for f in frames], frames[-1].get("cursor")) == \
        (["state", "log_history", "analysis"], 0)


async def test_attach_leaves_out_an_analysis_of_another_position(h, fake_engine):
    server = await fake_engine("analysis", query_delay=lambda q: 1.5 if q.get("moves") else 0.0)
    await h.connect_to(server)
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    await h.play("D4")
    assert [f["type"] for f in h.session.attach_frames()] == ["state", "log_history"]


# -- every frame is JSON ---------------------------------------------------------------------------
async def test_every_frame_is_valid_json_even_with_non_finite_engine_numbers(h, fake_engine):
    server = await fake_engine("analysis", nonfinite=True)
    await h.connect_to(server)
    await h.send({"type": "engine_params", "includeOwnership": True})
    await h.send({"type": "analysis", "enabled": True})
    assert await h.rec.wait("analysis") is not None
    await h.play("D4")
    await settle(0.5)
    assert h.rec.bad == []


# -- refusals stay short (§4.1, §7.6) ------------------------------------------------------------
HUGE = "D" * 900_000


@pytest.mark.parametrize("message", [
    {"type": "play", "color": "black", "vertex": HUGE},
    {"type": "new_game", "size": 9, "komi": None, "rules": HUGE, "handicap": 0},
    {"type": "board_select", "id": 10 ** 4000},
], ids=["vertex", "rules", "board-id"])
async def test_a_refusal_quotes_little_of_an_oversize_value(h, message):
    start = h.rec.mark()
    await h.send(message)
    error = await h.rec.wait_error(start)
    assert error is not None and len(error) <= 200


# -- names the page and the server must trim alike (§4.1) ----------------------------------------
BOM = "﻿"


async def test_a_preset_name_is_trimmed_as_the_page_trims_it(make_session):
    """§4.1 stores the trimmed name. The page trims with JavaScript's `trim()`, which takes
    U+FEFF, so a name ending in one must not be stored with it still there."""
    h = make_session(preferences={})
    await h.send({"type": "preferences",
                  "presets": [{"name": f"{BOM} opening {BOM}", "tuple": {"temperature": 2}}]})
    state = await h.fresh_state()
    assert [p["name"] for p in state["preferences"]["presets"]] == ["opening"]


async def test_a_board_name_is_trimmed_as_the_page_trims_it(h):
    """§3.3 trims a board name with the same rules."""
    board = h.rec.state()["activeBoard"]
    await h.send({"type": "board_rename", "id": board, "name": f"{BOM} study {BOM}"})
    state = await h.fresh_state()
    assert [b["name"] for b in state["boards"]] == ["study"]


async def test_a_board_rename_holding_only_a_bom_is_ignored(h):
    board = h.rec.state()["activeBoard"]
    before = h.rec.state()["boards"][0]["name"]
    await h.send({"type": "board_rename", "id": board, "name": BOM * 3})
    assert (await h.fresh_state())["boards"][0]["name"] == before


def test_the_trim_set_is_every_codepoint_str_strip_takes():
    """The set is written out so importing costs nothing on a CLI call, which means nothing keeps
    it honest but this. A Unicode revision that adds a space character must fail here rather than
    let the server and the page drift apart on a name's edges (§3.3, §4.1)."""
    from gowui.session import TRIM_ALSO, TRIM_CHARS

    runtime = {chr(c) for c in range(0x110000) if chr(c).isspace()}
    assert set(TRIM_CHARS) == runtime | {TRIM_ALSO}


@pytest.mark.parametrize("name", [
    " ﻿" * 262_132,
    "X" + " " * 524_260 + "X",
    "﻿ " * 131_066 + "X" + " ﻿" * 131_065,
], ids=["all-trimmable", "interior-run", "both-ends"])
async def test_a_name_is_trimmed_in_bounded_time(h, name):
    """§7.6 bounds a frame, not the work one costs, and a rename is the one handler that trims
    before it truncates, so the whole 1 MiB reaches the trim. Each shape below is the worst case
    for a different way of writing the trim: a convergence loop costs a pass per character on
    `all-trimmable`, and a pattern anchored to the end re-tries at every offset inside
    `interior-run`. One pass in from each end costs nothing on any of them. The session runs on a
    single event loop, so a handler that takes seconds denies service to every other account."""
    assert len(name.encode()) <= 1_048_576, "the frame would be refused for its size, not trimmed"
    board = h.rec.state()["activeBoard"]
    start = time.perf_counter()
    await h.send({"type": "board_rename", "id": board, "name": name})
    await h.fresh_state()
    assert time.perf_counter() - start < 0.2


async def test_a_later_preferences_change_leaves_a_frame_already_sent_alone(make_session):
    """§4.2: a `state` carries the preferences of the moment it was made, so the frame may share
    the stored value instead of copying it on every emission (the value is replaced, not
    changed in place)."""
    h = make_session(preferences={})
    await h.send({"type": "preferences", "lang": "ko",
                  "presets": [{"name": "first", "tuple": {"temperature": 2}}]})
    frame = h.session.attach_frames()[0]
    await h.send({"type": "preferences", "lang": "en",
                  "presets": [{"name": "second", "tuple": {"temperature": 2}}]})
    assert (frame["preferences"]["lang"],
            [p["name"] for p in frame["preferences"]["presets"]]) == ("ko", ["first"])
