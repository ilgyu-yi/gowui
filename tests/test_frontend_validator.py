"""The browser's tuple validator agrees with the server's (SPEC §2.5 "Policy tuples"; §3.8
"Human policy panel"; baseline feature B20).

``humanTuple.problem(tuple, visits)`` in ``tuple.js`` runs under ``node`` in a ``vm`` context with
a stub ``window`` whose ``i18n.t`` returns its key and variables as JSON, so its first problem can
be compared with Python's ``tuple_problem`` as a (rule, key) pair. The case table is the one
tests/test_handol_tuple.py uses for the server, plus the shapes only JSON text can produce.
"""

from __future__ import annotations

import json
import math
import re

import pytest

from frontend_helpers import node_or_skip, run_node, static_file
from gowui.engine.human import tuple_problem
from test_handol_tuple import INVALID, LAMBDA, VALID

EXTRA = [
    ({"lambda_utility": None, "min_p": 0.5}, 1),
    ({"distance_floor": 0.1, "distance_peak": 0.1}, 10),
    ({**LAMBDA, "temperature": 0.5, "distance_slope": 1}, 100),
    ({"temperature": -1, "min_p": 2}, 10),          # the first failing rule wins
    ({"foo": 1, "min_p": "x"}, 10),                 # an unknown key before a non-number
    ({"min_p": 0.1}, None),
    (dict(LAMBDA), None),
    ({"min_p": {"nested": 1}}, 10),
    ({"min_p": [0.1]}, 10),
]
CASES = VALID + INVALID + EXTRA


def python_rule(message: str | None) -> tuple[str, str | None] | None:
    """The rule a ``tuple_problem`` message reports, as ``(rule, key)``."""
    if message is None:
        return None
    patterns = [
        (r"^a policy tuple must be a JSON object$", "tuple.json.invalid"),
        (r"^unknown key '(.*)'$", "err.unknown"),
        (r"^(\w+) must be a finite number$", "err.number"),
        (r"^(\w+) must be greater than .*$", "err.gt"),
        (r"^(\w+) must be at least .*$", "err.ge"),
        (r"^(min_p) must be between 0 and 1$", "err.range"),
        (r"^lambda_utility requires trust_mu and fill_kappa$", "err.lambdaSubs"),
        (r"^trust_mu and fill_kappa require lambda_utility$", "err.subsWithoutLambda"),
        (r"^distance_floor must be below distance_peak$", "err.floorPeak"),
        (r"^lambda_utility needs a search .*$", "tuple.lambdaNeedsSearch"),
    ]
    for pattern, rule in patterns:
        found = re.match(pattern, message)
        if found:
            return rule, (found.group(1) if found.groups() else None)
    raise AssertionError(f"unmapped tuple_problem message: {message!r}")


def js_literal(value) -> str:
    """``value`` as a JS expression; NaN, ±Infinity and an overflowing int keep their meaning."""
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    if isinstance(value, float) and math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return "{" + ", ".join(f"{json.dumps(k)}: {js_literal(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ", ".join(js_literal(v) for v in value) + "]"
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2 ** 53:
        return "1e999"
    return json.dumps(value)


RUNNER = r"""
const vm = require('vm');
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const cases = eval(fs.readFileSync(0, 'utf8'));
const t = (key, vars) => JSON.stringify([key, vars || {}]);
const context = { i18n: { t: t }, localStorage: { getItem: () => null, setItem: () => {} },
                  navigator: { language: 'en' } };
context.window = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: 'tuple.js' });
const out = cases.map(([tuple, visits]) => {
  const found = context.humanTuple.problem(tuple, visits);
  if (found === null || found === undefined) return null;
  const [key, vars] = JSON.parse(found);
  let field = vars.field === undefined ? null : vars.field;
  if (typeof field === 'string' && field.startsWith('[')) field = JSON.parse(field)[0];
  if (typeof field === 'string' && field.startsWith('field.')) field = field.slice(6);
  return [key, field];
});
process.stdout.write(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def js_verdicts():
    """The browser validator's ``(rule, key)`` for every case, or the reason it could not run."""
    node_or_skip()
    if not static_file("js/tuple.js").is_file():
        return "gowui/static/js/tuple.js does not exist (SPEC §3.8)"
    cases = "[" + ", ".join(f"[{js_literal(t)}, {js_literal(v)}]" for t, v in CASES) + "]"
    result = run_node(["-e", RUNNER, str(static_file("js/tuple.js"))], stdin=cases)
    if result.returncode != 0:
        return f"node failed: {result.stderr[-1500:]}"
    return [None if v is None else tuple(v) for v in json.loads(result.stdout)]


@pytest.mark.parametrize("index", range(len(CASES)),
                         ids=[f"{json.dumps(t, default=str)[:40]}-visits{v}" for t, v in CASES])
def test_the_browser_validator_reports_the_rule_the_server_reports(js_verdicts, index):
    assert not isinstance(js_verdicts, str), js_verdicts
    tuple_, visits = CASES[index]
    assert js_verdicts[index] == python_rule(tuple_problem(tuple_, visits))

