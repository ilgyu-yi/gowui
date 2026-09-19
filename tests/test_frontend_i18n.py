"""The page's Korean and English tables (SPEC §3.8 "Language"; issue #8 AC2).

Every key the HTML and the scripts use exists in both the ``en`` and the ``ko`` table, and the two
tables hold the same keys. Keys are collected from the ``data-i18n`` / ``data-i18n-title`` /
``data-i18n-placeholder`` attributes of ``index.html`` and from every ``t(...)`` call in the
scripts: a literal key, a literal ternary, a literal key array (the candidate-table header), or a
dynamic family registered below. A ``t(...)`` call that is none of these fails the test, so a new
dynamic call site is registered here together with the keys it can produce.
"""

from __future__ import annotations

import re

import pytest

from frontend_helpers import (SCRIPTS, all_js, parse_tables, read_js, read_static,
                              strip_js_comments)

KEY_SHAPE = re.compile(r"^[A-Za-z][A-Za-z0-9]*(?:\.[A-Za-z0-9_]+)+$")
PLACEHOLDER = re.compile(r"\{(\w+)\}")

TUPLE_KEYS = ["lambda_utility", "trust_mu", "fill_kappa", "min_p", "distance_slope",
              "distance_floor", "distance_peak", "temperature"]
KNOBS = ["strength", "locality", "variety", "tail"]
PRESETS = ["identity", "483B", "lambdaLight", "lambdaStrong", "minP", "local", "localStrong",
           "sharp", "flat"]
FAMILIES = ["rank", "preaz", "proyear"]

#: ``t('<prefix>' + <expr> [+ '<suffix>'])`` call sites and the keys they can produce.
PREFIX_FAMILIES: dict[tuple[str, str], list[str]] = {
    ("field.", ""): TUPLE_KEYS,
    ("field.", ".help"): TUPLE_KEYS,
    ("field.", ".range"): TUPLE_KEYS,
    ("field.", ".example"): TUPLE_KEYS,
    ("knob.", ""): KNOBS,
    ("knob.", ".help"): KNOBS,
    ("knob.", ".lo"): KNOBS,
    ("knob.", ".hi"): KNOBS,
    ("preset.", ""): PRESETS,
    ("profile.group.", ""): FAMILIES,
    ("profile.groupdesc.", ""): FAMILIES,
}

#: Non-literal ``t(<expression>)`` call sites, by their exact expression, and their keys.
#: ``None``: the keys are covered elsewhere (a wrapper forwarding its parameter, the literal key
#: arrays, or the HTML attributes that ``i18n.apply`` reads).
DYNAMIC_SITES: dict[str, list[str] | None] = {
    "key": None,
    "node.getAttribute('data-i18n')": None,
    "node.getAttribute('data-i18n-title')": None,
    "node.getAttribute('data-i18n-placeholder')": None,
    "game.toPlay": ["black", "white"],
    "state.game.toPlay": ["black", "white"],
}

#: Keys §3.8 requires, with the English text where §3.8 pins it (``None``: any text).
REQUIRED_EN = {
    "status.lost": "Lost the connection to gowui; reconnecting...",
    "status.notSignedIn": None,     # the 4401 close (§3.8 "Connection")
    "status.refused": None,         # the 4403 close
    "noEngine": "no engine",
    "sgf.saved": "Saved {name}",
    "sgf.tooLarge": None,           # a file over 1 MiB, refused in the page (§3.8 "SGF")
    "sgf.loadFailed": None,         # any other load failure; names {status}
    "boards.default": "Board {n}",
    "thinking": "thinking",
    "disconnected": "disconnected",
}


def tables():
    parsed, _ = parse_tables(read_js("i18n.js"))
    return parsed


def table(lang: str) -> dict[str, str]:
    found = tables()
    if lang not in found:
        pytest.fail(f"i18n.js has no {lang!r} table in the strict shape")
    return dict(found[lang])


# -- call sites ------------------------------------------------------------------------------------
T_CALL = re.compile(r"(?<![\w$])t\s*\(")


def _argument(code: str, start: int) -> str:
    """The first argument of the call whose ``(`` is at ``start``."""
    depth, quote, i = 0, None, start + 1
    while i < len(code):
        c = code[i]
        if quote:
            if c == "\\":
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
        elif c in "([{":
            depth += 1
        elif c in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif c == "," and depth == 0:
            break
        i += 1
    return code[start + 1:i].strip()


def t_sites() -> list[tuple[str, str]]:
    """``(script, argument)`` for every ``t(...)`` / ``i18n.t(...)`` call (definitions skipped)."""
    sites = []
    for name, text in all_js().items():
        code = strip_js_comments(text)
        for match in T_CALL.finditer(code):
            before = code[max(0, match.start() - 12):match.start()]
            if re.search(r"function\s*$", before):
                continue
            paren = code.index("(", match.start())
            sites.append((name, _argument(code, paren)))
    return sites


LITERAL = re.compile(r"^'([^'\\]+)'$")
TERNARY = re.compile(r"^(?:[^'\"?]|'[^']*')*\?\s*'([^']+)'\s*:\s*'([^']+)'$")
PREFIXED = re.compile(r"^'([^']+)'\s*\+\s*[\w.\[\]]+(?:\s*\+\s*'([^']*)')?$")


def classify(argument: str) -> tuple[str, list[str] | None]:
    """``("literal" | "ternary" | "family" | "dynamic" | "unregistered", keys)``."""
    if m := LITERAL.match(argument):
        return "literal", [m.group(1)]
    if m := TERNARY.match(argument):
        return "ternary", [m.group(1), m.group(2)]
    if m := PREFIXED.match(argument):
        family = (m.group(1), m.group(2) or "")
        if family in PREFIX_FAMILIES:
            return "family", [family[0] + x + family[1] for x in PREFIX_FAMILIES[family]]
        return "unregistered", None
    if argument in DYNAMIC_SITES:
        return "dynamic", DYNAMIC_SITES[argument]
    return "unregistered", None


def used_keys() -> set[str]:
    keys: set[str] = set()
    html = read_static("index.html")
    for attribute in ("data-i18n", "data-i18n-title", "data-i18n-placeholder"):
        keys.update(re.findall(rf"\b{attribute}=\"([^\"]+)\"", html))
    for _, argument in t_sites():
        _, found = classify(argument)
        keys.update(found or [])
    for text in all_js().values():
        code = strip_js_comments(text)
        for array in re.findall(r"\[\s*'[^'\]]+'(?:\s*,\s*'[^'\]]+')*\s*\]", code):
            keys.update(k for k in re.findall(r"'([^']+)'", array) if KEY_SHAPE.match(k))
        keys.update(re.findall(r"setAttribute\s*\(\s*'data-i18n(?:-title|-placeholder)?'\s*,"
                               r"\s*'([^']+)'", code))
    return keys


# -- the tables ------------------------------------------------------------------------------------
def test_i18n_js_holds_an_en_and_a_ko_table():
    assert sorted(tables()) == ["en", "ko"]


def test_every_table_line_has_the_strict_key_value_shape():
    _, bad = parse_tables(read_js("i18n.js"))
    assert bad == []


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_no_key_appears_twice_in_a_table(lang):
    entries = tables().get(lang, [])
    keys = [k for k, _ in entries]
    assert sorted({k for k in keys if keys.count(k) > 1}) == []


def test_every_english_key_is_in_the_korean_table():
    assert sorted(set(table("en")) - set(table("ko"))) == []


def test_every_korean_key_is_in_the_english_table():
    assert sorted(set(table("ko")) - set(table("en"))) == []


def test_a_key_has_the_same_placeholders_in_both_tables():
    en, ko = table("en"), table("ko")
    differ = sorted(k for k in set(en) & set(ko)
                    if set(PLACEHOLDER.findall(en[k])) != set(PLACEHOLDER.findall(ko[k])))
    assert differ == []


def test_the_tables_are_not_trivially_small():
    """Count guard: the baseline tables hold over 200 keys each."""
    assert min(len(table("en")), len(table("ko"))) > 200


# -- keys used by the page ---------------------------------------------------------------------------
@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_key_the_html_uses_is_in_the_table(lang):
    html = read_static("index.html")
    keys = set()
    for attribute in ("data-i18n", "data-i18n-title", "data-i18n-placeholder"):
        keys.update(re.findall(rf"\b{attribute}=\"([^\"]+)\"", html))
    assert keys, "index.html uses no data-i18n keys"
    assert sorted(keys - set(table(lang))) == []


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_key_the_scripts_use_is_in_the_table(lang):
    assert sorted(used_keys() - set(table(lang))) == []


def test_every_t_call_names_its_key_as_a_literal_or_a_registered_family():
    unregistered = [f"{name}: t({argument})" for name, argument in t_sites()
                    if classify(argument)[0] == "unregistered"]
    assert unregistered == []


def test_the_scripts_call_t_with_literal_keys_often():
    """Count guard against a regex that collects nothing: the baseline has over 60 literal
    ``t('…')`` calls."""
    literal = [a for _, a in t_sites() if classify(a)[0] in ("literal", "ternary")]
    assert len(literal) > 60


@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_but_board_js_translates_through_t(name):
    """Every script but the canvas one shows text; each must reach the tables through ``t``."""
    if name == "board.js":
        pytest.skip("board.js may draw without text from the tables")
    assert [a for n, a in t_sites() if n == name]


def test_the_candidate_table_header_keys_are_in_both_tables():
    keys = {"col.move", "col.win", "col.score", "col.visits", "col.policy", "col.prob", "col.a",
            "col.b", "col.delta"}
    en, ko = table("en"), table("ko")
    assert sorted(k for k in keys if k not in en or k not in ko) == []


def test_the_candidate_table_header_keys_are_used():
    assert sorted({"col.prob", "col.a", "col.b", "col.delta"} - used_keys()) == []


# -- texts §3.8 requires ------------------------------------------------------------------------------
@pytest.mark.parametrize("key", sorted(REQUIRED_EN))
def test_a_required_key_is_in_both_tables(key):
    assert (key in table("en"), key in table("ko")) == (True, True)


@pytest.mark.parametrize("key", sorted(k for k, v in REQUIRED_EN.items() if v is not None))
def test_a_required_english_text_reads_as_the_spec_says(key):
    assert table("en").get(key) == REQUIRED_EN[key]


def test_the_korean_default_board_name_is_bo_deu_n():
    assert table("ko").get("boards.default") == "보드 {n}"


def test_the_load_failure_text_names_the_http_status():
    assert ("{status}" in table("en").get("sgf.loadFailed", ""),
            "{status}" in table("ko").get("sgf.loadFailed", "")) == (True, True)


def test_the_two_close_reasons_read_differently():
    en = table("en")
    assert en.get("status.notSignedIn") != en.get("status.refused")


@pytest.mark.parametrize("key", ["status.lost", "status.notSignedIn", "status.refused",
                                 "sgf.tooLarge", "sgf.loadFailed"])
def test_a_status_key_is_used_by_the_scripts(key):
    assert key in used_keys()
