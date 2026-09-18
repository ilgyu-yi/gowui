"""The engine package surface: one error kind (SPEC §2.1), the shared analysis shape (§2.2) and the
protocol registry."""

from __future__ import annotations

import json

import pytest

from gowui.engine import (PROTOCOLS, Analysis, AnalysisEngine, ConnectionClosed, Engine,
                          EngineError, GTPEngine, MoveInfo, Position, RootInfo, create_engine)

ANALYSIS_KEYS = {"moveInfos", "rootInfo", "ownership", "policy", "turn", "complete", "source",
                 "currentPlayer", "compare"}
MOVE_INFO_KEYS = {"move", "visits", "winrate", "scoreLead", "scoreMean", "scoreStdev", "prior",
                  "lcb", "utility", "utilityLcb", "order", "pv"}
ROOT_INFO_KEYS = {"visits", "winrate", "scoreLead", "scoreMean"}


# -- one error kind ----------------------------------------------------------------
def test_connection_closed_is_an_engine_error():
    assert issubclass(ConnectionClosed, EngineError)


def test_engine_error_carries_the_address_outside_its_message():
    error = EngineError("engine refused the connection", address=("10.1.2.3", 6363))
    assert error.address == ("10.1.2.3", 6363)


def test_engine_error_message_does_not_contain_the_address():
    error = EngineError("engine refused the connection", address=("10.1.2.3", 6363))
    assert "10.1.2.3" not in str(error) and "6363" not in str(error)


def test_engine_error_address_defaults_to_none():
    assert EngineError("boom").address is None


# -- shared analysis shape ------------------------------------------------------------
def test_analysis_to_dict_has_exactly_the_spec_keys():
    assert set(Analysis().to_dict()) == ANALYSIS_KEYS


def test_move_info_to_dict_has_exactly_the_spec_keys():
    assert set(MoveInfo(move="D4").to_dict()) == MOVE_INFO_KEYS


def test_root_info_to_dict_has_exactly_the_spec_keys():
    assert set(RootInfo().to_dict()) == ROOT_INFO_KEYS


def test_analysis_nests_move_infos_and_root_info():
    shape = Analysis(move_infos=[MoveInfo(move="D4", visits=3, pv=["D4", "Q16"])],
                     root=RootInfo(visits=3)).to_dict()
    assert (shape["moveInfos"][0]["pv"], shape["rootInfo"]["visits"]) == (["D4", "Q16"], 3)


def test_an_empty_analysis_is_valid_json():
    json.dumps(Analysis().to_dict(), allow_nan=False)


def test_compare_is_none_unless_a_protocol_sets_it():
    assert Analysis().to_dict()["compare"] is None


# -- positions -------------------------------------------------------------------------
def test_position_holds_the_whole_position():
    position = Position(size=9, komi=6.5, rules="japanese", initial_stones=[["B", "C3"]],
                        moves=[["W", "E5"]], first_player="W")
    assert (position.size, position.komi, position.rules, position.initial_stones,
            position.moves, position.first_player) == (9, 6.5, "japanese", [["B", "C3"]],
                                                       [["W", "E5"]], "W")


# -- protocol registry ------------------------------------------------------------------
def test_gtp_and_analysis_are_registered_protocols():
    assert {"gtp", "analysis"} <= set(PROTOCOLS)


@pytest.mark.parametrize("protocol, cls", [("gtp", GTPEngine), ("analysis", AnalysisEngine)])
def test_create_engine_builds_the_protocols_client(protocol, cls):
    assert isinstance(create_engine(protocol, "127.0.0.1", 1), cls)


def test_every_client_is_an_engine():
    assert all(isinstance(create_engine(p, "127.0.0.1", 1), Engine) for p in PROTOCOLS)


def test_create_engine_refuses_an_unknown_protocol():
    with pytest.raises(EngineError):
        create_engine("telepathy", "127.0.0.1", 1)


def test_gtp_client_capabilities():
    engine = create_engine("gtp", "127.0.0.1", 1)
    assert (engine.supports_genmove, engine.supports_final_score, engine.supports_raw) == (
        True, True, True)


def test_analysis_client_capabilities():
    engine = create_engine("analysis", "127.0.0.1", 1)
    assert (engine.supports_genmove, engine.supports_final_score, engine.supports_raw) == (
        True, False, False)
