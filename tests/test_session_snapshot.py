"""Snapshots (SPEC §8.1, §6.3, §7.6, §7.7): the version 1 shape, a lossless round trip, a forgiving
restore that enforces the limits, refusal of any other version, and ``resume`` replaying the
stored engine request through the engine-address policy.
"""

from __future__ import annotations

import copy
import json

import pytest

from session_helpers import (LOOPBACK, CatalogResolver, board_entry, board_ids, move_list,
                             settle, sgf_of)


async def configured(h) -> None:
    """A space with two boards, moves, a cursor in the past, names and non-default settings."""
    await h.play("D4", "E5", "F6")
    await h.send({"type": "navigate", "index": 2})
    await h.send({"type": "human_params", "profile": "rank_5k", "policy": {"min_p": 0.05},
                  "compare": {"temperature": 2}, "evalVisits": 50})
    await h.send({"type": "board_rename", "id": h.rec.state()["activeBoard"], "name": "study"})
    await h.send({"type": "board_duplicate"})
    await h.send({"type": "engine_params", "maxVisits": 321, "reportInterval": 0.7,
                  "includeOwnership": True})
    await h.send({"type": "players", "blackIsEngine": True, "whiteStyle": "katago"})
    await h.send({"type": "analysis", "enabled": True})
    await h.fresh_state()


def restored(make_session, data: dict, resolver=None, **kwargs):
    other = make_session(resolver, **kwargs)
    other.session.restore(copy.deepcopy(data))
    return other


# -- the version 1 shape ---------------------------------------------------------------------------
async def test_a_snapshot_has_the_version_1_top_level_keys(h):
    data = h.session.snapshot()
    assert (sorted(data), data["version"]) == \
        (["activeBoard", "boards", "engine", "play", "version"], 1)


async def test_a_snapshot_board_has_the_version_1_keys(h):
    assert sorted(h.session.snapshot()["boards"][0]) == \
        ["cursor", "humanCompare", "humanPolicy", "humanProfile", "id", "name", "sgf"]


async def test_a_snapshot_engine_block_has_the_version_1_keys(h):
    assert sorted(h.session.snapshot()["engine"]) == \
        ["connected", "evalVisits", "includeOwnership", "maxVisits", "reportInterval", "request"]


async def test_a_snapshot_play_block_has_the_version_1_keys(h):
    assert sorted(h.session.snapshot()["play"]) == \
        ["analysisEnabled", "blackIsEngine", "blackStyle", "whiteIsEngine", "whiteStyle"]


async def test_a_snapshot_is_json(h):
    await configured(h)
    data = h.session.snapshot()
    assert json.loads(json.dumps(data, allow_nan=False)) == data


# -- round trip ------------------------------------------------------------------------------------
async def test_a_snapshot_round_trips_through_restore(h, make_session):
    await configured(h)
    data = h.session.snapshot()
    assert restored(make_session, data).session.snapshot() == data


async def test_a_restored_space_shows_the_active_boards_game_and_cursor(h, make_session):
    await configured(h)
    data = h.session.snapshot()
    state = await restored(make_session, data).fresh_state()
    assert (state["activeBoard"], move_list(state), state["game"]["cursor"]) == \
        (data["activeBoard"], ["D4", "E5", "F6"], 2)


async def test_a_connected_snapshot_stores_the_accepted_request(h, gtp_server):
    await h.connect_to(gtp_server)
    engine = h.session.snapshot()["engine"]
    assert (engine["connected"], engine["request"]) == \
        (True, {"protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port})


async def test_a_catalog_snapshot_stores_only_the_engine_id(make_session, gtp_server):
    catalog = CatalogResolver()
    catalog.add("kata", "gtp", gtp_server.port, console=True)
    h = make_session(catalog, expose_address=False)
    await h.connect({"engineId": "kata"})
    data = h.session.snapshot()
    text = json.dumps(data)
    assert (data["engine"]["request"], LOOPBACK in text, str(gtp_server.port) in text) == \
        ({"engineId": "kata"}, False, False)


# -- a forgiving restore (§8.1) --------------------------------------------------------------------
def with_boards(data: dict, boards: list[dict]) -> dict:
    data = copy.deepcopy(data)
    data["boards"] = boards
    data["activeBoard"] = boards[0]["id"]
    return data


def board(board_id, sgf: str = "", **fields) -> dict:
    entry = {"id": board_id, "name": f"Board {board_id}", "sgf": sgf or sgf_of(9), "cursor": 0,
             "humanProfile": "preaz_1d", "humanPolicy": {}, "humanCompare": None}
    entry.update(fields)
    return entry


@pytest.mark.parametrize("sgf", ["not an sgf at all", "(;SZ[99])",
                                 "(;GM[1]SZ[9];B[cc]C[" + "x" * (1024 * 1024) + "])"],
                         ids=["garbage", "bad-size", "over-1-mib"])
async def test_a_board_whose_sgf_does_not_read_comes_back_empty(h, make_session, sgf):
    data = with_boards(h.session.snapshot(), [board(1, sgf_of(9, "D4")), board(2, sgf)])
    state = await restored(make_session, data).fresh_state()
    assert [b["moveCount"] for b in state["boards"]] == [1, 0]


async def test_a_restore_keeps_the_first_64_boards(h, make_session):
    boards = [board(i) for i in range(1, 71)]
    data = with_boards(h.session.snapshot(), boards)
    assert board_ids(await restored(make_session, data).fresh_state()) == list(range(1, 65))


async def test_duplicate_and_non_positive_board_ids_are_replaced(h, make_session):
    data = with_boards(h.session.snapshot(), [board(3), board(3), board(0), board(-4)])
    ids = board_ids(await restored(make_session, data).fresh_state())
    assert (ids[0], len(set(ids)), all(i > 0 for i in ids)) == (3, 4, True)


async def test_a_restored_board_name_is_truncated_to_40_characters(h, make_session):
    data = with_boards(h.session.snapshot(), [board(1, name="n" * 50)])
    state = await restored(make_session, data).fresh_state()
    assert board_entry(state, 1)["name"] == "n" * 40


@pytest.mark.parametrize("tuple_", [{"min_p": 5}, {"surprise": 1}, "wide", [1], None],
                         ids=["range", "unknown-key", "string", "list", "null"])
async def test_an_invalid_restored_tuple_becomes_empty(h, make_session, tuple_):
    data = with_boards(h.session.snapshot(), [board(1, humanPolicy=tuple_)])
    state = await restored(make_session, data).fresh_state()
    assert state["settings"]["humanPolicy"] == {}


@pytest.mark.parametrize("profile", ["bad name!", "", "p" * 65, 7, None])
async def test_an_invalid_restored_profile_becomes_the_default(h, make_session, profile):
    default = h.session.snapshot()["boards"][0]["humanProfile"]
    data = with_boards(h.session.snapshot(), [board(1, humanProfile=profile)])
    state = await restored(make_session, data).fresh_state()
    assert state["settings"]["humanProfile"] == default


async def test_a_restored_cursor_is_kept(h, make_session):
    data = with_boards(h.session.snapshot(), [board(1, sgf_of(9, "D4", "E5"), cursor=1)])
    assert (await restored(make_session, data).fresh_state())["game"]["cursor"] == 1


BAD_SETTINGS = [
    ("engine", "maxVisits", -5), ("engine", "maxVisits", 0), ("engine", "maxVisits", 5_000_000),
    ("engine", "maxVisits", "abc"), ("engine", "maxVisits", True),
    ("engine", "reportInterval", 100), ("engine", "reportInterval", 0.01),
    ("engine", "reportInterval", "fast"), ("engine", "includeOwnership", "yes"),
    ("engine", "evalVisits", -1), ("engine", "evalVisits", True),
    ("play", "blackIsEngine", "yes"), ("play", "blackStyle", "robot"),
    ("play", "analysisEnabled", 3),
]


@pytest.mark.parametrize("block, key, value", BAD_SETTINGS,
                         ids=[f"{b}.{k}={v!r}" for b, k, v in BAD_SETTINGS])
async def test_an_unknown_or_out_of_range_setting_falls_back_to_the_default(
        h, make_session, block, key, value):
    defaults = h.session.snapshot()
    data = copy.deepcopy(defaults)
    data[block][key] = value
    assert restored(make_session, data).session.snapshot()[block][key] == defaults[block][key]


# -- other versions are refused whole --------------------------------------------------------------
BAD_VERSIONS = {"2": 2, "0": 0, "string": "1", "null": None, "float": 1.5, "bool": True}


@pytest.mark.parametrize("name", sorted(BAD_VERSIONS) + ["missing"])
async def test_a_snapshot_of_another_version_is_refused(h, make_session, name):
    data = h.session.snapshot()
    if name == "missing":
        del data["version"]
    else:
        data["version"] = BAD_VERSIONS[name]
    with pytest.raises(ValueError):
        make_session().session.restore(data)


async def test_a_refused_snapshot_changes_nothing(h, make_session):
    await configured(h)
    data = h.session.snapshot()
    data["version"] = 2
    other = make_session()
    before = other.session.snapshot()
    with pytest.raises(ValueError):
        other.session.restore(data)
    assert other.session.snapshot() == before


# -- resume: the stored request is replayed through the policy (§6.3, §8.1) ------------------------
async def test_resume_reconnects_a_connected_snapshot(h, make_session, gtp_server):
    await h.connect_to(gtp_server)
    data = h.session.snapshot()
    other = restored(make_session, data)
    await other.session.resume()
    state = await other.rec.wait_state(lambda f: f["engine"]["connected"])
    assert state is not None and state["engine"]["request"] == data["engine"]["request"]


async def test_resume_leaves_a_disconnected_snapshot_disconnected(h, make_session, gtp_server):
    data = h.session.snapshot()
    data["engine"]["request"] = {"protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port}
    other = restored(make_session, data)
    await other.session.resume()
    await settle(0.5)
    assert gtp_server.open_connections == 0


async def test_resume_with_a_request_the_policy_no_longer_accepts_stays_disconnected(
        make_session, gtp_server):
    catalog = CatalogResolver()
    catalog.add("kata", "gtp", gtp_server.port, console=True)
    h = make_session(catalog, expose_address=False)
    await h.connect({"engineId": "kata"})
    data = h.session.snapshot()
    shrunk = CatalogResolver()
    other = restored(make_session, data, shrunk, expose_address=False)
    await other.session.resume()
    state = await other.fresh_state()
    assert (state["engine"]["connected"], state["status"] != "") == (False, True)


# -- the restored request (§4.2, §8.1, §7.7) -------------------------------------------------------
def nested(depth: int) -> dict:
    value: dict = {"engineId": "kata"}
    for _ in range(depth):
        value = {"engineId": "kata", "next": value}
    return value


BAD_REQUESTS = {
    "deeply-nested": nested(5000),
    "nested-once": {"engineId": "kata", "extra": {"a": 1}},
    "list-value": {"engineId": ["kata"]},
    "17-entries": {f"k{i}": i for i in range(17)},
    "long-string": {"engineId": "k" * 257},
    "not-an-object": ["kata"],
}


@pytest.mark.parametrize("name", sorted(BAD_REQUESTS))
async def test_a_restored_request_that_is_not_flat_and_small_becomes_null(h, make_session, name):
    data = h.session.snapshot()
    data["engine"]["request"] = BAD_REQUESTS[name]
    data["engine"]["connected"] = True
    other = make_session()
    other.session.restore(data)
    assert other.session.snapshot()["engine"]["request"] is None


async def test_a_flat_restored_request_is_kept(h, make_session):
    data = h.session.snapshot()
    request = {f"k{i}": v for i, v in enumerate(["s" * 256, 1, 2.5, True, None] * 3)}
    data["engine"]["request"] = request
    assert restored(make_session, data).session.snapshot()["engine"]["request"] == request


def hidden_snapshot(h, request: dict) -> dict:
    data = h.session.snapshot()
    data["engine"]["request"] = request
    data["engine"]["connected"] = True
    return data


async def test_a_hidden_space_shows_no_request_before_the_replay_resolves(h, make_session,
                                                                         gtp_server):
    catalog = CatalogResolver()
    catalog.add("kata", "gtp", gtp_server.port)
    data = hidden_snapshot(h, {"engineId": "kata", "note": "gowui-secret.invalid"})
    other = restored(make_session, data, catalog, expose_address=False)
    assert (await other.fresh_state())["engine"]["request"] is None


async def test_a_hidden_space_shows_only_the_policys_echo_after_the_replay(h, make_session,
                                                                          gtp_server):
    catalog = CatalogResolver()
    catalog.add("kata", "gtp", gtp_server.port)
    data = hidden_snapshot(h, {"engineId": "kata", "note": "gowui-secret.invalid"})
    other = restored(make_session, data, catalog, expose_address=False)
    await other.session.resume()
    state = await other.rec.wait_state(lambda f: f["engine"]["connected"])
    assert state is not None and state["engine"]["request"] == {"engineId": "kata"}


async def test_a_refused_replay_clears_the_request_and_drops_it_from_the_snapshot(
        h, make_session):
    data = hidden_snapshot(h, {"engineId": "kata", "host": "gowui-secret.invalid"})
    other = restored(make_session, data, CatalogResolver(), expose_address=False)
    await other.session.resume()
    state = await other.fresh_state()
    assert (state["engine"]["request"], other.session.snapshot()["engine"]["request"]) == \
        (None, None)


async def test_a_local_space_shows_the_restored_request(h, make_session, gtp_server):
    request = {"protocol": "gtp", "host": LOOPBACK, "port": gtp_server.port}
    data = hidden_snapshot(h, request)
    data["engine"]["connected"] = False
    assert (await restored(make_session, data).fresh_state())["engine"]["request"] == request
