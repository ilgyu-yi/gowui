"""Continuous analysis through the session (SPEC §3.2, §3.3, §4.2): it follows the cursor, stops
when switched off or when the game is over at the cursor, never shows a report for a position
the user has left, analyses only the board on screen, and thumbnails keep their last heatmap and
winrate only while those describe the board's position.
"""

from __future__ import annotations

import json

import pytest

from helpers import wait_for
from session_helpers import settle, sgf_of


def analysis_frames(h, start: int = 0) -> list[dict]:
    return h.rec.of("analysis", start)


async def analysing(h, server) -> None:
    await h.connect_to(server)
    await h.send({"type": "analysis", "enabled": True})


async def first_analysis(h, predicate=lambda f: True, start: int = 0) -> dict:
    frame = await h.rec.wait("analysis", predicate, start)
    assert frame is not None, f"no analysis frame; errors: {h.rec.errors(start)}"
    return frame


def queries_since(server, index: int) -> list[dict]:
    """Analysis-engine position queries the fake received after request number ``index``."""
    out = []
    for line in server.requests[index:]:
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and "action" not in value:
            out.append(value)
    return out


# -- the position under the cursor ------------------------------------------------------------
@pytest.mark.parametrize("protocol", ["gtp", "analysis", "handol"])
async def test_analysis_reports_the_position_under_the_cursor(h, fake_engine, protocol):
    server = await fake_engine(protocol)
    await analysing(h, server)
    frame = await first_analysis(h)
    assert (frame["cursor"], frame["toPlay"], bool(frame["analysis"]["moveInfos"])) == \
        (0, "black", True)


async def test_analysis_restarts_after_a_move(h, analysis_server):
    await analysing(h, analysis_server)
    await first_analysis(h)
    start = h.rec.mark()
    await h.play("D4")
    await first_analysis(h, lambda f: f["cursor"] == 1 and f["toPlay"] == "white", start)


async def test_analysis_restarts_after_navigation(h, analysis_server):
    await h.play("D4", "E5")
    await analysing(h, analysis_server)
    await first_analysis(h, lambda f: f["cursor"] == 2)
    start = h.rec.mark()
    await h.send({"type": "navigate", "index": 1})
    await first_analysis(h, lambda f: f["cursor"] == 1, start)


async def test_analysis_restarts_after_undo(h, analysis_server):
    await h.play("D4", "E5")
    await analysing(h, analysis_server)
    await first_analysis(h, lambda f: f["cursor"] == 2)
    start = h.rec.mark()
    await h.send({"type": "undo"})
    await first_analysis(h, lambda f: f["cursor"] == 1, start)


async def test_analysis_stops_when_switched_off(h, analysis_server):
    await analysing(h, analysis_server)
    await first_analysis(h)
    await h.send({"type": "analysis", "enabled": False})
    await settle(0.5)
    start = h.rec.mark()
    await settle(1.0)
    assert analysis_frames(h, start) == []


async def test_every_analysis_frame_describes_its_cursor(h, fake_engine):
    server = await fake_engine("analysis", query_delay=lambda q: 0.3)
    await analysing(h, server)
    await h.play("D4", "E5", "F6")
    await h.send({"type": "navigate", "index": 1})
    await settle(1.5)
    frames = analysis_frames(h)
    assert frames and all(f["analysis"]["turn"] == f["cursor"] for f in frames)


# -- stale reports -------------------------------------------------------------------------------
async def test_no_report_for_the_previous_cursor_after_a_move(h, fake_engine):
    server = await fake_engine("analysis", ignore_interrupt=True,
                               query_delay=lambda q: 0.6 if not q.get("moves") else 0.0)
    await analysing(h, server)
    await h.play("D4")
    after = h.rec.mark()
    await settle(1.5)
    assert [f for f in analysis_frames(h, after) if f["cursor"] != 1] == []


async def test_no_report_for_a_position_left_by_undo_and_a_different_move(h, fake_engine):
    """Same cursor, different position: the late report for [D4] (whose candidates start with
    E5, fake engine determinism) must not be shown once E5 has been played instead."""
    server = await fake_engine(
        "analysis", ignore_interrupt=True,
        query_delay=lambda q: 0.8 if q.get("moves") == [["B", "D4"]] else 0.0)
    await analysing(h, server)
    await h.play("D4")
    await h.send({"type": "undo"})
    await h.play("E5")
    after = h.rec.mark()
    await settle(1.8)
    stale = [f for f in analysis_frames(h, after)
             if any(m["move"] == "E5" for m in f["analysis"]["moveInfos"])]
    assert stale == []


# -- game over at the cursor (§1.3) --------------------------------------------------------------
async def test_a_game_with_a_scored_result_is_analysed(h, analysis_server):
    await analysing(h, analysis_server)
    start = h.rec.mark()
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4", "E5", "F6", result="B+3.5")})
    await first_analysis(h, lambda f: f["cursor"] == 3, start)


async def test_a_resigned_game_is_not_analysed_at_its_end(h, analysis_server):
    await analysing(h, analysis_server)
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4", "E5", "F6", result="W+R")})
    await settle(0.5)
    start = h.rec.mark()
    await settle(1.0)
    assert analysis_frames(h, start) == []


async def test_a_resigned_game_is_analysed_at_an_earlier_cursor(h, analysis_server):
    await analysing(h, analysis_server)
    await h.send({"type": "load_sgf", "sgf": sgf_of(9, "D4", "E5", "F6", result="W+R")})
    start = h.rec.mark()
    await h.send({"type": "navigate", "index": 2})
    await first_analysis(h, lambda f: f["cursor"] == 2, start)


async def test_analysis_stops_after_two_passes(h, analysis_server):
    await analysing(h, analysis_server)
    await first_analysis(h)
    await h.send({"type": "pass"})
    await h.send({"type": "pass"})
    await settle(0.5)
    start = h.rec.mark()
    await settle(1.0)
    assert analysis_frames(h, start) == []


# -- boards ---------------------------------------------------------------------------------------
async def test_only_the_board_on_screen_is_analysed(h, analysis_server):
    await h.play("D4")
    await analysing(h, analysis_server)
    await first_analysis(h)
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    assert await h.rec.wait_state(lambda f: len(f["boards"]) == 2, start)
    index = len(analysis_server.requests)
    await h.play("E5")
    both = [["B", "D4"], ["W", "E5"]]
    # A query for the one-move position may still be in flight; count from the first E5 query.
    assert await wait_for(lambda: any(q["moves"] == both for q in queries_since(analysis_server, index)))
    queries = queries_since(analysis_server, index)
    first = next(i for i, q in enumerate(queries) if q["moves"] == both)
    await settle(1.0)
    moves = [q["moves"] for q in queries_since(analysis_server, index)[first:]]
    assert moves and all(m == both for m in moves)


async def test_a_report_for_the_board_left_is_not_shown_on_the_new_one(h, fake_engine):
    server = await fake_engine("analysis", ignore_interrupt=True,
                               query_delay=lambda q: 0.8 if len(q.get("moves", [])) == 1 else 0.0)
    await h.play("D4")
    await analysing(h, server)
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    assert await h.rec.wait_state(lambda f: len(f["boards"]) == 2, start)
    await h.play("E5")
    after = h.rec.mark()
    await settle(1.5)
    assert [f for f in analysis_frames(h, after) if f["cursor"] != 2] == []


async def test_a_thumbnail_keeps_its_heatmap_after_switching_away(h, analysis_server):
    await analysing(h, analysis_server)
    await first_analysis(h, lambda f: bool(f["analysis"]["policy"]))
    board_a = h.rec.state()["activeBoard"]
    start = h.rec.mark()
    await h.send({"type": "board_duplicate"})
    state = await h.rec.wait_state(lambda f: f["activeBoard"] != board_a, start)
    assert state is not None
    thumb = next(b for b in state["boards"] if b["id"] == board_a)
    assert bool(thumb["heat"]) and isinstance(thumb["winrate"], float)


async def test_a_thumbnail_drops_its_heatmap_when_the_position_changes(h, fake_engine):
    server = await fake_engine("analysis",
                               query_delay=lambda q: 1.5 if q.get("moves") else 0.0)
    await analysing(h, server)
    await first_analysis(h, lambda f: bool(f["analysis"]["policy"]))
    board_a = h.rec.state()["activeBoard"]
    state = await h.play("D4")
    thumb = next(b for b in state["boards"] if b["id"] == board_a)
    assert (thumb["heat"], thumb["winrate"]) == ([], None)


# -- analysis refreshes are coalesced (§3.2) -------------------------------------------------------
async def test_a_flood_of_analysis_messages_keeps_the_task_count_bounded(h, fake_engine):
    from session_helpers import gowui_tasks, gtp_count

    server = await fake_engine("gtp", delay={"genmove": 1.0})
    await h.connect_to(server)
    await h.send({"type": "genmove"})
    assert await wait_for(lambda: gtp_count(server, "genmove") == 1)
    for index in range(300):
        await h.send({"type": "analysis", "enabled": index % 2 == 1})
    during = len(gowui_tasks())
    start = h.rec.mark()
    frame = await first_analysis(h, lambda f: f["cursor"] == 1, start)
    assert (during <= 5, frame["cursor"]) == (True, 1)
