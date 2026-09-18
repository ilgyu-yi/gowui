"""Rule sets (SPEC §1.1), named the way KataGo names them.

The ``katago`` field is passed to engines unchanged (``kata-set-rules``, the analysis ``rules``
field), so the engine scores the game the way gowui adjudicates it.
"""

from __future__ import annotations

from dataclasses import dataclass

SIMPLE_KO = "simple"
POSITIONAL_SUPERKO = "positional"
SITUATIONAL_SUPERKO = "situational"


@dataclass(frozen=True)
class RuleSet:
    name: str
    katago: str
    ko: str
    suicide: bool
    default_komi: float
    scoring: str  # "area" or "territory"


RULE_SETS: dict[str, RuleSet] = {
    rs.name: rs
    for rs in [
        RuleSet("japanese", "japanese", SIMPLE_KO, False, 6.5, "territory"),
        RuleSet("korean", "korean", SIMPLE_KO, False, 6.5, "territory"),
        RuleSet("chinese", "chinese", POSITIONAL_SUPERKO, False, 7.5, "area"),
        RuleSet("aga", "aga", SITUATIONAL_SUPERKO, False, 7.5, "area"),
        RuleSet("new-zealand", "new-zealand", SITUATIONAL_SUPERKO, True, 7.0, "area"),
        RuleSet("tromp-taylor", "tromp-taylor", POSITIONAL_SUPERKO, True, 7.5, "area"),
    ]
}

DEFAULT_RULES = "japanese"

#: Handicap games default to a half-point komi (SPEC §1.2).
HANDICAP_KOMI = 0.5


def get_rules(name: str) -> RuleSet:
    """Return the rule set called ``name``; an unknown name raises ``ValueError``."""
    key = name.strip().lower() if isinstance(name, str) else None
    if key not in RULE_SETS:
        raise ValueError(f"unknown rule set {name!r}; known: {', '.join(sorted(RULE_SETS))}")
    return RULE_SETS[key]
