"""Static checks of the page's sources (SPEC §3.8 "The page", "Rendering safety"; §7.5; §8.5).

They need no browser. The syntax check runs ``node --check`` on every script (issue #8 AC1); it
is skipped without ``node`` locally and fails without it in CI.
"""

from __future__ import annotations

import re

import pytest

from frontend_helpers import (SCRIPTS, SOCKET_SEND, SWITCH_TYPE, all_js, read_js, read_static,
                              run_node, static_file, strip_js_comments)


def index() -> str:
    return read_static("index.html")


def tags(html: str, name: str) -> list[str]:
    return re.findall(rf"<{name}\b[^>]*>", html, flags=re.IGNORECASE)


def attr(tag: str, name: str) -> str | None:
    found = re.search(rf"\b{name}\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", tag, flags=re.IGNORECASE)
    return found.group(1).strip("\"'") if found else None


def element(html: str, element_id: str) -> str:
    """The opening tag of the element with ``element_id``; fails when there is none."""
    found = re.search(rf"<[a-z]+\b[^>]*\bid=\"{re.escape(element_id)}\"[^>]*>", html)
    if not found:
        pytest.fail(f"index.html has no element with id {element_id!r} (SPEC §3.8)")
    return found.group(0)


def options(html: str, select_id: str) -> list[tuple[str, bool]]:
    """``(value, selected)`` of each option of the select ``select_id`` (value = text if none)."""
    found = re.search(rf"<select\b[^>]*\bid=\"{re.escape(select_id)}\"[^>]*>(.*?)</select>",
                      html, flags=re.DOTALL)
    if not found:
        pytest.fail(f"index.html has no select with id {select_id!r} (SPEC §3.8)")
    out = []
    for opening, text in re.findall(r"(<option\b[^>]*>)(.*?)</option>", found.group(1),
                                    flags=re.DOTALL):
        value = attr(opening, "value")
        out.append((value if value is not None else text.strip(),
                    re.search(r"\bselected\b", opening) is not None))
    return out


# -- the files (§3.8, §5) ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", SCRIPTS)
def test_each_script_of_the_page_exists(name):
    assert static_file(f"js/{name}").is_file(), f"gowui/static/js/{name} is missing (SPEC §3.8)"


def test_the_stylesheet_exists():
    assert static_file("css/style.css").is_file()


def test_the_icon_is_a_file():
    assert static_file("favicon.svg").is_file()


@pytest.mark.parametrize("name", SCRIPTS)
def test_every_script_parses_with_node_check(name):
    """Issue #8 AC1: every JS file parses (``node --check``)."""
    path = static_file(f"js/{name}")
    result = run_node(["--check", str(path)])
    assert result.returncode == 0, result.stderr[-2000:]


def test_the_page_loads_the_five_scripts_in_order():
    sources = [attr(tag, "src") for tag in tags(index(), "script")]
    assert sources == [f"/js/{name}" for name in SCRIPTS]


def test_the_page_links_one_stylesheet():
    sheets = [attr(tag, "href") for tag in tags(index(), "link")
              if (attr(tag, "rel") or "").lower() == "stylesheet"]
    assert sheets == ["/css/style.css"]


def test_the_page_icon_is_the_favicon_file():
    icons = [attr(tag, "href") for tag in tags(index(), "link")
             if "icon" in (attr(tag, "rel") or "").lower().split()]
    assert icons == ["/favicon.svg"]


# -- rendering safety (§3.8, §7.5) ----------------------------------------------------------------------
def test_the_page_has_no_inline_script():
    html = index()
    inline = [m for m in re.findall(r"<script\b([^>]*)>(.*?)</script>", html,
                                    flags=re.DOTALL | re.IGNORECASE)
              if attr(m[0], "src") is None or m[1].strip()]
    assert inline == [] and tags(html, "script"), "no inline scripts, and the page has scripts"


def test_the_page_has_no_style_element():
    assert tags(index(), "style") == []


def test_the_page_has_no_style_attribute():
    assert re.findall(r"<[^>]*\sstyle\s*=", index(), flags=re.IGNORECASE) == []


def test_the_page_has_no_event_handler_attribute():
    assert re.findall(r"<[^>]*\son[a-z]+\s*=", index(), flags=re.IGNORECASE) == []


def test_the_page_has_no_data_or_javascript_url():
    assert re.findall(r"\b(?:data|javascript)\s*:", index(), flags=re.IGNORECASE) == []


HTML_SINKS = [
    r"\.innerHTML\b", r"\.outerHTML\b", r"\binsertAdjacentHTML\b", r"\bdocument\.write(?:ln)?\b",
    r"(?<![\w.$])eval\s*\(", r"\bnew\s+Function\b", r"(?<![\w.$])Function\s*\(",
    r"\bsetTimeout\s*\(\s*['\"`]", r"\bsetInterval\s*\(\s*['\"`]",
]


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_uses_an_html_sink_or_evaluates_text(name):
    code = strip_js_comments(read_js(name))
    found = [pattern for pattern in HTML_SINKS if re.search(pattern, code)]
    assert found == []


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_writes_a_style_attribute(name):
    """Only CSSOM property writes are allowed (§3.8); a ``style`` attribute would break the
    policy."""
    code = strip_js_comments(read_js(name))
    found = re.findall(r"setAttribute\s*\(\s*['\"]style['\"]|\.style\.cssText\b", code)
    assert found == []


# -- the candidate readout (§3.8 "Candidate readout", "PV preview") -----------------------------------
def test_board_js_takes_no_text_from_the_i18n_tables():
    """§3.8: the readout line is "the only place a candidate's full set is written: a preview puts
    no second copy of these numbers on the board". The board's caption was the one thing board.js
    drew from the tables (``visits.count``), and a caption that is gone leaves no lookup behind.
    Neither the DOM nor the draw record can see a caption painted on the canvas, so the source is
    where this rule is readable at all."""
    assert re.search(r"\bi18n\b", strip_js_comments(read_js("app.js"))), \
        "the scan finds no i18n in app.js, so finding none in board.js would say nothing"
    assert re.findall(r"\bi18n\b", strip_js_comments(read_js("board.js"))) == []


# -- browser storage (§8.5) ---------------------------------------------------------------------------
def test_the_page_stores_exactly_the_two_browser_keys():
    literals = set()
    for text in all_js().values():
        literals.update(re.findall(r"['\"](gowui\.[A-Za-z0-9_.]+)['\"]", strip_js_comments(text)))
    assert literals == {"gowui.lang", "gowui.userPresets"}


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_script_uses_session_storage_or_cookies_or_indexeddb(name):
    code = strip_js_comments(read_js(name))
    assert re.findall(r"\bsessionStorage\b|\bdocument\.cookie\b|\bindexedDB\b", code) == []


def block_after(code: str, pattern: str) -> str:
    """The ``{...}`` block whose opening brace follows the first match of ``pattern``."""
    match = re.search(pattern, code)
    assert match, f"app.js has nothing matching {pattern}"
    start = code.index("{", match.end())
    depth = 0
    for i in range(start, len(code)):
        if code[i] == "{":
            depth += 1
        elif code[i] == "}":
            depth -= 1
            if depth == 0:
                return code[start:i + 1]
    return code[start:]


def test_the_page_never_reconnects_after_a_4401_4403_or_4429_close():
    """§3.8 "Connection": those three closes are the ones no retry can lift — a handshake past the
    identity's socket cap (§4.3) included — so each returns from ``onclose`` before the reconnect
    timer, and the status keeps saying why."""
    code = strip_js_comments(read_js("app.js"))
    body = block_after(code, r"socket\.onclose\s*=\s*function\s*\([^)]*\)\s*")
    reconnect = body.find("setTimeout(connect")
    assert reconnect > 0, "app.js does not reconnect with setTimeout(connect, ...)"
    final = body[:reconnect]
    assert sorted(int(c) for c in re.findall(r"event\.code === (\d+)", final)) == [4401, 4403, 4429]
    assert final.count("return;") == 3


def test_the_4429_close_shows_its_own_reason():
    """§3.8 "Connection": the socket-cap close says why it will not retry, so the page does not
    sit on the lost-connection text while nothing reconnects."""
    code = strip_js_comments(read_js("app.js"))
    branch = block_after(code, r"if\s*\(\s*event\.code === 4429\s*\)")
    key = re.search(r"closedFor = '([\w.]+)'", branch)
    assert key, "the 4429 branch sets no closedFor key"
    shown = block_after(code, r"function\s+showConnectionProblem\s*\(\s*\)\s*")
    assert f"t('{key.group(1)}')" in shown
    assert key.group(1) in read_js("i18n.js")


def test_a_4401_close_sends_the_page_to_the_root():
    """§3.8 "Connection": on ``4401`` the page goes to ``/`` and lets the guard choose between
    the sign-in page and the 401 page; it never names ``/login`` itself."""
    code = strip_js_comments(read_js("app.js"))
    assert re.search(r"location\.assign\(\s*'/'\s*\)", code)
    assert "/login" not in code


# -- structure the protocol and i18n checks rely on (tests/frontend_helpers.py) ------------------------
def test_app_js_defines_one_send():
    code = strip_js_comments(read_js("app.js"))
    assert len(re.findall(r"\bfunction\s+send\s*\(", code)) == 1


def test_a_socket_send_appears_exactly_once_in_the_scripts():
    """Every frame goes through one ``send`` (§3.8 "Connection"): ``.send(`` on the socket is
    written once, inside it."""
    count = sum(len(SOCKET_SEND.findall(strip_js_comments(text))) for text in all_js().values())
    assert count == 1


def test_the_socket_send_is_inside_the_send_function():
    code = strip_js_comments(read_js("app.js"))
    start = re.search(r"\bfunction\s+send\s*\(", code)
    assert start, "app.js has no function send"
    body = code[start.end():]
    depth, end = 0, len(body)
    for i, c in enumerate(body):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    assert SOCKET_SEND.search(body[:end]) is not None


def test_the_page_handles_frames_in_one_switch_on_the_type():
    count = sum(len(SWITCH_TYPE.findall(strip_js_comments(text))) for text in all_js().values())
    assert count == 1


# -- layout (§3.8 "Layout", "Controls") ---------------------------------------------------------------
def sections(html: str) -> list[tuple[str, bool]]:
    """``(summary key, open)`` of each top-level ``<details>`` section, in page order (nested
    ``<details>``, such as Raw values, are skipped)."""
    out: list[tuple[str, bool]] = []
    depth, opening = 0, ""
    for m in re.finditer(r"(<details\b[^>]*>)|</details>|<summary\b[^>]*data-i18n=\"([^\"]+)\"",
                         html):
        if m.group(1):
            depth += 1
            opening = m.group(1)
        elif m.group(0) == "</details>":
            depth -= 1
        elif depth == 1:
            out.append((m.group(2), re.search(r"\bopen\b", opening) is not None))
    return out


def test_the_side_panel_sections_come_in_the_spec_order():
    assert [key for key, _ in sections(index())] == [
        "human.section", "analysis.section", "players.section", "newGame.section",
        "moves.section", "console.section"]


def test_human_policy_analysis_and_players_start_open_and_the_others_closed():
    assert sections(index()) == [("human.section", True), ("analysis.section", True),
                                 ("players.section", True), ("newGame.section", False),
                                 ("moves.section", False), ("console.section", False)]


def test_the_rules_select_offers_exactly_the_rule_sets_with_japanese_selected():
    from gowui.rules import DEFAULT_RULES, RULE_SETS

    offered = options(index(), "new-rules")
    assert (sorted(v for v, _ in offered), [v for v, s in offered if s]) == \
        (sorted(RULE_SETS), [DEFAULT_RULES])


def test_the_size_select_offers_9_13_19_with_19_selected():
    assert options(index(), "new-size") == [("9", False), ("13", False), ("19", True)]


def test_the_handicap_field_ranges_from_0_to_9():
    tag = element(index(), "new-handicap")
    assert (attr(tag, "min"), attr(tag, "max")) == ("0", "9")


def test_the_protocol_select_offers_the_three_protocols():
    assert [v for v, _ in options(index(), "protocol")] == ["gtp", "analysis", "handol"]


def test_the_label_select_offers_the_four_label_modes():
    assert [v for v, _ in options(index(), "label-mode")] == ["winrate", "visits", "prior", "score"]


def test_the_compare_show_select_offers_a_b_and_the_difference():
    assert [v for v, _ in options(index(), "compare-view")] == ["A", "B", "diff"]


def test_the_move_style_selects_offer_human_and_katago():
    assert [[v for v, _ in options(index(), s)] for s in ("black-style", "white-style")] == \
        [["human", "katago"], ["human", "katago"]]


def test_the_language_select_offers_korean_and_english():
    found = re.search(r"<select\b[^>]*\bid=\"lang\"[^>]*>(.*?)</select>", index(), flags=re.DOTALL)
    assert found, "index.html has no #lang select"
    pairs = re.findall(r"<option\b[^>]*value=\"([^\"]+)\"[^>]*>(.*?)</option>", found.group(1))
    assert pairs == [("ko", "한국어"), ("en", "English")]


def test_the_sgf_picker_accepts_sgf_files():
    assert ".sgf" in (attr(element(index(), "sgf-file"), "accept") or "")


# Every id the browser tests drive (tests/browser); §3.8 names what each one is.
PAGE_IDS = [
    "protocol", "host", "port", "connect", "engine-state", "lang", "status",
    "board-list", "board-new", "board",
    "candidate-readout",
    "first", "prev10", "prev", "move-counter", "next", "next10", "last", "pass", "undo", "resign",
    "winbar-black", "winbar-label", "to-play", "score-lead", "visit-count",
    "human-profile", "profile-help", "eval-visits", "compare-on", "tuple-tabs", "compare-view",
    "human-preset", "preset-save", "preset-delete", "preset-export", "preset-import",
    "preset-file", "tuple-knobs", "live-apply", "tuple-fields", "human-policy", "tuple-problem",
    "analysis-on", "max-visits", "interval", "label-mode", "show-ownership", "show-policy",
    "show-numbers", "candidates",
    "black-engine", "white-engine", "black-style", "white-style", "genmove", "final-score",
    "captures-black", "captures-white",
    "new-size", "new-handicap", "new-komi", "new-rules", "new-game", "save-sgf", "load-sgf",
    "sgf-file", "move-list", "log", "raw-form", "raw",
]


@pytest.mark.parametrize("element_id", PAGE_IDS)
def test_the_page_has_the_element(element_id):
    assert re.search(rf"\bid=\"{re.escape(element_id)}\"", index()), \
        f"index.html has no element with id {element_id!r}"


def test_no_id_is_used_twice():
    ids = re.findall(r"\bid=\"([^\"]+)\"", index())
    assert sorted({i for i in ids if ids.count(i) > 1}) == []
