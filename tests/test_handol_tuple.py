"""The handol-mux policy-tuple rules (SPEC §2.5 "Policy tuples", "Settings validation"), checked
on the shared pure validator the client and the session both use."""

from __future__ import annotations

import math

import pytest

from gowui.engine import EngineError
from gowui.engine.human import (DEFAULT_DISTANCE_FLOOR, DEFAULT_DISTANCE_PEAK, MOVE_STYLES,
                                TUPLE_KEYS, check_policies, tuple_problem)

LAMBDA = {"lambda_utility": 0.1, "trust_mu": 0.05, "fill_kappa": 1}


# -- constants -------------------------------------------------------------------------------
def test_the_tuple_keys_are_exactly_the_eight_mux_keys():
    assert set(TUPLE_KEYS) == {"lambda_utility", "trust_mu", "fill_kappa", "min_p",
                               "distance_slope", "distance_floor", "distance_peak",
                               "temperature"}


def test_the_tuple_keys_have_no_duplicates():
    assert len(TUPLE_KEYS) == 8


def test_the_default_distance_floor_is_one_tenth():
    assert DEFAULT_DISTANCE_FLOOR == 0.1


def test_the_default_distance_peak_is_one_and_a_half():
    assert DEFAULT_DISTANCE_PEAK == 1.5


def test_the_move_styles_are_human_and_katago():
    assert set(MOVE_STYLES) == {"human", "katago"}


# -- valid tuples ---------------------------------------------------------------------------------
VALID = [
    ({}, 1),
    ({}, 400),
    ({"min_p": 0}, 1),
    ({"min_p": 1}, 1),
    ({"min_p": 0.05}, 1),
    ({"distance_slope": 0}, 1),
    ({"distance_slope": 0.25}, 1),
    ({"distance_floor": 1.4}, 1),          # below the default peak 1.5
    ({"distance_peak": 0.2}, 1),           # above the default floor 0.1
    ({"distance_floor": 2, "distance_peak": 3}, 1),
    ({"temperature": 0.00011}, 1),
    ({"temperature": 1}, 1),               # an int is a number
    ({"lambda_utility": None}, 1),         # null lambda counts as absent
    (dict(LAMBDA), 2),                     # lambda with a search
    ({**LAMBDA, "fill_kappa": 0}, 50),
    ({**LAMBDA, "min_p": 0.05}, 400),
]


@pytest.mark.parametrize("tuple_, visits", VALID)
def test_a_valid_tuple_has_no_problem(tuple_, visits):
    assert tuple_problem(tuple_, visits) is None


# -- refused tuples -------------------------------------------------------------------------------
INVALID = [
    # not a JSON object
    (None, 10),
    ([], 10),
    ("{}", 10),
    (1, 10),
    # unknown key
    ({"foo": 1}, 10),
    ({"min_p": 0.1, "maxVisits": 10}, 10),
    # non-numbers: bool, null (except lambda), strings, non-finite, overflowing
    ({"min_p": True}, 10),
    ({"temperature": False}, 10),
    ({"min_p": None}, 10),
    ({"temperature": None}, 10),
    ({"temperature": "1"}, 10),
    ({"temperature": math.nan}, 10),
    ({"temperature": math.inf}, 10),
    ({"distance_slope": -math.inf}, 10),
    ({"temperature": 10 ** 400}, 10),
    ({"lambda_utility": True, "trust_mu": 0.05, "fill_kappa": 1}, 10),
    # ranges
    ({**LAMBDA, "lambda_utility": 0}, 10),
    ({**LAMBDA, "lambda_utility": -0.1}, 10),
    ({**LAMBDA, "trust_mu": 0}, 10),
    ({**LAMBDA, "fill_kappa": -0.01}, 10),
    ({"min_p": -0.01}, 10),
    ({"min_p": 1.01}, 10),
    ({"distance_slope": -0.1}, 10),
    ({"distance_floor": 0}, 10),
    ({"distance_peak": 0}, 10),
    ({"temperature": 0.0001}, 10),
    ({"temperature": 0}, 10),
    # lambda requires trust_mu and fill_kappa, and they require it
    ({"lambda_utility": 0.1}, 10),
    ({"lambda_utility": 0.1, "trust_mu": 0.05}, 10),
    ({"lambda_utility": 0.1, "fill_kappa": 1}, 10),
    ({"trust_mu": 0.05}, 10),
    ({"fill_kappa": 1}, 10),
    ({"trust_mu": 0.05, "fill_kappa": 1}, 10),
    ({"lambda_utility": None, "trust_mu": 0.05, "fill_kappa": 1}, 10),
    # distance_floor < distance_peak, with the defaults filling in
    ({"distance_floor": 1.5}, 10),
    ({"distance_peak": 0.1}, 10),
    ({"distance_floor": 2, "distance_peak": 1}, 10),
    ({"distance_floor": 1, "distance_peak": 1}, 10),
    # lambda needs a search
    (dict(LAMBDA), 1),
    (dict(LAMBDA), 0),
]


@pytest.mark.parametrize("tuple_, visits", INVALID)
def test_a_refused_tuple_names_a_problem(tuple_, visits):
    problem = tuple_problem(tuple_, visits)
    assert isinstance(problem, str) and problem.strip()


# -- one or two tuples ---------------------------------------------------------------------------
@pytest.mark.parametrize("policies", [[{}], [{}, {"min_p": 0.1}], [dict(LAMBDA), {}]])
def test_one_or_two_valid_tuples_pass(policies):
    check_policies(policies, 10)  # raises EngineError on a refusal


@pytest.mark.parametrize("policies", [
    [],
    [{}, {}, {}],
    [{"foo": 1}],
    [{}, {"foo": 1}],
    [{}, {"lambda_utility": 0.1}],
    {},
    None,
])
def test_no_tuple_more_than_two_or_a_bad_one_is_an_engine_error(policies):
    with pytest.raises(EngineError):
        check_policies(policies, 10)


def test_a_lambda_tuple_without_a_search_is_an_engine_error():
    with pytest.raises(EngineError):
        check_policies([dict(LAMBDA)], 1)


def test_the_policy_error_carries_no_address():
    with pytest.raises(EngineError) as caught:
        check_policies([{"foo": 1}], 10)
    assert caught.value.address is None
