"""Page rules that only a browser can show (SPEC §3.8, §8.5; issue #22).

Each test drives the page's own controls and frames, never its internals: the status line after a
``4403`` close, a rename field while its tile moves, the PV preview of a candidate the board did
not draw, the ring on the stone just played, an empty Visits field, the presets this browser
has stored, and what the page may claim while it has no engine (#66): the engine failures in red
past the timer, the analysis and tile figures gone with the engine, a bar that draws no position,
the three play settings cleared and inert, and the empty catalog said beside the picker.

The frames the server would not send on cue (a reordered ``state``, an ``analysis``) go in through
``proxy_ws`` / ``inject`` of browser_kit.py.
"""

from __future__ import annotations

import base64
import json
import math
import re
import socket
import types

import pytest

from browser_kit import ENGINE, QUICK, Gowui, analysis_frame, analysis_payload, expect, move_info, vertex

pytestmark = pytest.mark.browser

#: The draw record (§3.8 "Test observability") naming the point the last-move ring went round,
#: empty when the draw ringed nothing. The Code phase has to add it; §3.8 "Last move" is not
#: observable without it.
RING = "lastMoveRing"


def hover_point(g: Gowui, x: int, y: int, size: int) -> None:
    """Rest the pointer on point (``x``, ``y``) counted from the top left (board.js geometry)."""
    box = g.page.locator("#board").bounding_box()
    margin = box["width"] / (size + 1.6)
    cell = (box["width"] - 2 * margin) / (size - 1)
    scale = box["height"] / box["width"]
    g.page.mouse.move(box["x"] + margin + x * cell, box["y"] + (margin + y * cell) * scale)


def after_a_draw(g: Gowui, before: int) -> None:
    """Wait until the board has drawn again, so a record read after it is the new one."""
    g.until("(before) => Number(document.getElementById('board').dataset.draws) > before", before)


# -- the status line (§3.8 "Status line", "Connection") ------------------------------------------
def test_a_refused_close_keeps_saying_why(start_app, open_page, tmp_path):
    """§3.8: a ``4403`` reason stands until the page is reloaded — a later message neither
    replaces it nor starts a timer over it."""
    g = open_page(start_app())
    g.allow_console("WebSocket")
    g.proxy_ws()
    g.open()
    refused = g.t("status.refused")

    g.routes[-1].close(code=4403)
    expect(g.page.locator("#status")).to_have_text(refused, timeout=QUICK)

    # A file over 1 MiB is refused in the page itself (§3.8 "SGF"): a status message that needs
    # no socket, so it arrives while the reason stands.
    big = tmp_path / "big.sgf"
    big.write_text("(;GM[1]FF[4]SZ[19]C[" + "x" * 1_100_000 + "])", encoding="utf-8")
    g.page.locator("#sgf-file").set_input_files(str(big))
    g.page.wait_for_timeout(500)
    assert g.page.locator("#status").text_content() == refused


# -- the board strip (§3.8 "Rename in place") ----------------------------------------------------
def test_a_tile_move_does_not_save_an_open_rename(start_app, open_page):
    """§3.8: only Enter or the user leaving the field saves. A ``state`` whose board order moved
    the tile takes the field's focus away, and that must save nothing."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.act({"type": "board_duplicate", "id": g.state()["activeBoard"]})
    expect(g.page.locator("#board-list .thumb")).to_have_count(2, timeout=QUICK)

    state = g.state()
    ids = [board["id"] for board in state["boards"]]
    tile = g.page.locator(f'#board-list .thumb[data-id="{ids[1]}"]')
    tile.locator(".thumb-edit").click()
    field = tile.locator(".thumb-rename")
    field.fill("half typed")

    since = g.mark()
    g.inject({**state, "boards": list(reversed(state["boards"]))})
    g.until("(id) => document.querySelectorAll('#board-list .thumb')[0].dataset.id === id",
            str(ids[1]))

    g.fence()
    assert g.sent(since, "board_rename") == []
    assert field.input_value() == "half typed"
    assert g.page.evaluate("() => document.activeElement.className") == "thumb-rename"


def test_the_last_live_heatmap_stays_on_a_thumbnail_after_switching(start_app, open_page):
    """§4.2: an analysis updates the active thumbnail cache. The next state sends only the new
    active drawing, so the board left must keep the winrate that proves its analysis was cached."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    first = g.state()["activeBoard"]
    second = g.act({"type": "board_duplicate", "id": first})["activeBoard"]
    g.act({"type": "board_select", "id": first})

    state = g.state()
    size = state["game"]["size"]
    policy = [0.0] * (size * size + 1)
    policy[0] = 1.0
    g.inject(analysis_frame(state, analysis_payload(size, [], winrate=0.73, policy=policy)))
    meta = g.page.locator(f'#board-list .thumb[data-id="{first}"] .thumb-meta:not(.thumb-tuple)')
    expect(meta).to_contain_text("73%", timeout=QUICK)

    g.act({"type": "board_select", "id": second})
    expect(meta).to_contain_text("73%", timeout=QUICK)


# -- candidates and the PV preview (§3.8 "Board overlays") ---------------------------------------
def test_only_a_drawn_candidate_previews_its_pv(start_app, open_page):
    """§3.8: at most the first 12 ``moveInfos`` are drawn, and a later entry is not on the board,
    so hovering its point previews nothing."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    size = g.state()["game"]["size"]
    infos = [move_info(vertex(x, 0, size), visits=100 - x,
                       pv=[vertex(x, 0, size), vertex(x, 1, size)]) for x in range(14)]
    g.inject(analysis_frame(g.state(), analysis_payload(size, infos)))
    g.expect_dataset("candidates", "12")

    # The 13th candidate: drawn nowhere, so its point previews nothing.
    before = g.draws()
    hover_point(g, 12, 0, size)
    after_a_draw(g, before)
    assert (g.dataset("preview"), g.dataset("candidates")) == ("", "12")

    # The first one, to show the hover itself reaches the board.
    before = g.draws()
    hover_point(g, 0, 0, size)
    after_a_draw(g, before)
    assert (g.dataset("preview"), g.dataset("candidates")) == (vertex(0, 0, size), "0")


# -- the last-move ring (§3.8 "Last move") -------------------------------------------------------
def play(g: Gowui, move: str = "D4", color: str = "black") -> str:
    """Play ``move`` and wait until the board has drawn the position it makes."""
    before = g.draws()
    g.act({"type": "play", "color": color, "vertex": move})
    after_a_draw(g, before)
    return move


def test_the_stone_just_played_is_ringed(start_app, open_page):
    """§3.8 "Last move": the stone just played carries a ring around its edge."""
    g = open_page(start_app()).open()
    played = play(g)
    assert g.dataset(RING) == played


def test_the_ring_stays_when_move_numbers_are_ticked(start_app, open_page):
    """§3.8 "Last move": the ring is drawn whether or not Move numbers are ticked — the mark and
    the numbers no longer take turns, since the ring leaves the centre to the number."""
    g = open_page(start_app()).open()
    played = play(g)
    before = g.draws()
    g.page.locator("#show-numbers").check()
    after_a_draw(g, before)
    assert (g.dataset("numbers"), g.dataset(RING)) == ("on", played)


def test_nothing_is_ringed_with_no_move_yet_or_after_a_pass(start_app, open_page):
    """§3.8 "Last move": nothing is ringed when the game has no moves yet or the last move was a
    pass — the ring marks a stone, and neither of those put one down."""
    g = open_page(start_app()).open()
    fresh = g.dataset(RING)
    played = play(g)
    ringed = g.dataset(RING)

    before = g.draws()
    g.act({"type": "pass", "color": "white"})
    after_a_draw(g, before)
    assert (fresh, ringed, g.dataset(RING)) == ("", played, "")


def test_a_pv_preview_rings_none_of_its_own_stones(start_app, open_page):
    """§3.8 "Last move": the ring belongs to the position, not to a variation laid over it, so a
    preview's stones are never ringed and the position's last move keeps the only ring."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    played = play(g)
    size = g.state()["game"]["size"]
    candidate = vertex(0, 0, size)
    infos = [move_info(candidate, pv=[candidate, vertex(1, 0, size)])]
    g.inject(analysis_frame(g.state(), analysis_payload(size, infos)))
    g.expect_dataset("candidates", "1")

    before = g.draws()
    hover_point(g, 0, 0, size)
    after_a_draw(g, before)
    assert (g.dataset("preview"), g.dataset(RING)) == (candidate, played)


# -- engine parameters (§3.8 "Controls") ---------------------------------------------------------
@pytest.mark.parametrize("typed", ["", "0", "1.9"])
def test_a_visits_field_without_a_usable_number_sends_no_visit_change(start_app, open_page,
                                                                      typed):
    """§3.8: an empty Visits field, or one that is not a whole number of at least 1, sends no
    visit change; the setting keeps its value and the field is filled again from the next
    ``state``. A typed ``1.9`` is such a field: it is not truncated to 1, a number the user
    never typed."""
    g = open_page(start_app()).open()
    was = g.state()["settings"]["maxVisits"]
    field = g.page.locator("#max-visits")

    field.fill(typed)
    since = g.mark()
    field.dispatch_event("change")
    frame = g.wait_sent("engine_params", since)
    assert "maxVisits" not in frame

    field.blur()
    g.fence()
    assert g.state()["settings"]["maxVisits"] == was
    expect(field).to_have_value(str(was), timeout=QUICK)


@pytest.mark.parametrize("typed", ["", "0"])
def test_an_every_field_without_a_usable_number_sends_no_interval_change(start_app, open_page,
                                                                         typed):
    """§3.8: the Every field is held like Visits — an empty one, or a ``0``, sends no interval
    change instead of the page's own 0.4, so the setting keeps the value it had."""
    g = open_page(start_app()).open()
    was = g.state()["settings"]["reportInterval"]
    field = g.page.locator("#interval")

    field.fill(typed)
    since = g.mark()
    field.dispatch_event("change")
    frame = g.wait_sent("engine_params", since)
    assert "reportInterval" not in frame
    assert "includeOwnership" in frame, "the frame's other fields still go"

    field.blur()
    g.fence()
    assert g.state()["settings"]["reportInterval"] == was
    expect(field).to_have_value(str(was), timeout=QUICK)


def test_an_every_field_with_a_number_still_sends_it(start_app, open_page):
    g = open_page(start_app()).open()
    field = g.page.locator("#interval")
    field.fill("0.8")
    since = g.mark()
    field.dispatch_event("change")
    assert g.wait_sent("engine_params", since)["reportInterval"] == 0.8
    field.blur()
    g.fence()
    assert g.state()["settings"]["reportInterval"] == 0.8


def test_a_visits_field_with_a_number_still_sends_it(start_app, open_page):
    g = open_page(start_app()).open()
    field = g.page.locator("#max-visits")
    field.fill("700")
    since = g.mark()
    field.dispatch_event("change")
    assert g.wait_sent("engine_params", since)["maxVisits"] == 700
    field.blur()
    g.fence()
    assert g.state()["settings"]["maxVisits"] == 700


def test_an_unusable_visits_edit_checks_lambda_against_the_setting_in_force(start_app, open_page):
    """The field names no setting while it is unusable, so lambda is checked against the last
    ``state`` rather than against the field's temporary lack of a number."""
    g = open_page(start_app()).open()
    g.page.locator("#protocol").select_option("handol")
    g.page.locator("#human-preset").select_option("builtin:lambdaLight")
    expect(g.page.locator("#tuple-problem")).to_be_hidden()

    g.page.locator("#max-visits").fill("1.9")
    g.page.locator("#human-policy").dispatch_event("input")
    expect(g.page.locator("#tuple-problem")).to_be_hidden()


# -- stored presets (§8.5) -----------------------------------------------------------------------
def test_a_stored_preset_the_tuple_rules_refuse_is_dropped(start_app, open_page):
    """§8.5: an entry without a name, or with a tuple §2.5 refuses, is dropped when the list is
    read — the menu offers only the presets that can be sent."""
    stored = [
        {"name": "keeps", "tuple": {"min_p": 0.1}},
        {"name": "lambda", "tuple": {"lambda_utility": 0.5, "trust_mu": 1, "fill_kappa": 0}},
        {"name": "out of range", "tuple": {"min_p": 2}},
        {"name": "unknown key", "tuple": {"nonsense": 1}},
        {"name": "not a number", "tuple": {"temperature": "warm"}},
        {"name": "  ", "tuple": {}},
    ]
    g = open_page(start_app(), storage={"gowui.userPresets": json.dumps(stored)}).open()
    values = g.page.eval_on_selector_all(
        "#human-preset option", "options => options.map((option) => option.value)")
    assert [v for v in values if v.startswith("user:")] == ["user:keeps", "user:lambda"]


# -- the candidate table and the readout (§3.8 "Candidate table", "Candidate readout") -----------
def head_fields(g: Gowui) -> list[str]:
    """The current head's column keys, without their ``col.`` prefix — the table's own order, so
    a cell is read by the field it holds and not by a number the test would have to keep."""
    return g.page.evaluate("""() => [...document.querySelectorAll('table.candidates thead th')]
        .map((th) => th.getAttribute('data-i18n').slice(4))""")


def table_cells(g: Gowui, move: str) -> dict[str, str]:
    """The row for ``move``, by column key. Empty when the table has no such row."""
    texts = g.page.evaluate("""(move) => {
        const row = [...document.querySelectorAll('#candidates tr')].find(
            (tr) => tr.cells[0].textContent.trim() === move);
        return row ? [...row.cells].map((c) => c.textContent.trim()) : [];
    }""", move)
    return dict(zip(head_fields(g), texts))


def readout_field(g: Gowui, name: str):
    """The readout's ``name`` field, or ``None`` when the line has no such field."""
    return g.page.evaluate("""(name) => {
        const node = document.querySelector('#candidate-readout [data-field="' + name + '"]');
        return node ? node.textContent.trim() : null;
    }""", name)


def show_view(g: Gowui, view: str) -> None:
    """Switch the comparison view through the page's own select (§3.8). Its row is hidden until a
    handol-mux engine turns comparing on, which an injected frame does not do, so the test unhides
    the row and then uses the control."""
    g.page.evaluate("""() => {
        document.querySelectorAll('.human-only').forEach((section) => {
            section.hidden = false;
            section.open = true;
        });
        document.getElementById('compare-view-wrap').hidden = false;
    }""")
    g.page.locator("#compare-view").select_option(view)


def test_a_visit_count_the_engine_did_not_report_is_a_dash(start_app, open_page):
    """§3.8 "Candidate readout": a value the engine did not report is ``-``, never a zero — and
    never the word ``null``. The abbreviation the table and the line share is written for a
    number, and hands back whatever it is given as a string, so a missing count has to be caught
    before it: `visits` takes that path like every other field."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    size = g.state()["game"]["size"]
    move = vertex(0, 0, size)
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, [move_info(move, visits=None)])))
    expect(g.page.locator("#candidates tr")).to_have_count(1, timeout=QUICK)

    assert table_cells(g, move)["visits"] == "-", table_cells(g, move)
    assert readout_field(g, "visits") == "-"


def test_every_view_shows_a_candidate_the_search_it_had(start_app, open_page):
    """§3.8 "Candidate table": Visits is in every mode — it is what the search spent, and the
    search spent it on the position, not on a view of the position. So a candidate the search
    knows carries the same visits, Value and bound under A, under B and under B − A; only the
    columns the view is about (A, B, Δ) change. A point the search never looked at carries ``-``,
    which is the same rule: a value the engine did not report is never a zero."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    size = g.state()["game"]["size"]
    known, unknown = vertex(0, 0, size), vertex(5, 5, size)
    searched = {**move_info(known, visits=1000, prior=0.2),
                "utility": 0.42, "utilityLcb": 0.31}
    a = [0.0] * (size * size + 1)
    b = [0.0] * (size * size + 1)
    a[0], b[0] = 0.2, 0.9                       # the known candidate: Δ +70.0%, the largest
    b[5 * size + 5] = 0.5                       # a point tuple B likes and the search never saw
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, [searched], source="handol", policy=a,
        # Tuple B is a distribution, not a search: it reports no visit count of its own, so what
        # the line shows for it can only be the search that was run (§3.8).
        compare={"policy": b, "moveInfos": [{**move_info(known, visits=None, prior=0.9),
                                             "utility": None, "utilityLcb": None}]})))
    expect(g.page.locator("table.candidates thead th")).to_have_count(8, timeout=QUICK)

    seen = {}
    for view in ("A", "B", "diff"):
        show_view(g, view)
        expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=QUICK)
        seen[view] = table_cells(g, known)
    assert [seen[view].get("visits") for view in ("A", "B", "diff")] == ["1.0k"] * 3, seen
    assert [seen[view].get("value") for view in ("A", "B", "diff")] == ["+0.42"] * 3, seen

    # Still in B − A: the point only tuple B likes, and the line on the candidate it does know.
    assert table_cells(g, unknown)["visits"] == "-", table_cells(g, unknown)
    assert table_cells(g, unknown)["value"] == "-", table_cells(g, unknown)
    assert (readout_field(g, "move"), readout_field(g, "visits"),
            readout_field(g, "value"), readout_field(g, "valueLcb")) == (
        known, "1.0k", "B+0.42", "B+0.31")


# -- what the candidate circles paint (§3.8 "Top candidates") ------------------------------------
#: Wrap `fillText` and the paint setters on the 2D context prototype, so a draw's text and colours
#: can be read back. A canvas is otherwise write-only to a test: `-` and `undefined` are both
#: merely pixels to a screenshot. The assigned value is recorded, not the normalised property.
RECORDER = """() => {
    const proto = CanvasRenderingContext2D.prototype;
    window.__paint = {text: [], fill: [], stroke: [], arc: [], currentFill: '', currentStroke: ''};
    const fillText = proto.fillText;
    proto.fillText = function (text) {
        if (this.canvas.id === 'board' && window.__paint) {
            window.__paint.text.push([String(text), window.__paint.currentFill]);
        }
        return fillText.apply(this, arguments);
    };
    const own = Object.getOwnPropertyDescriptor(proto, 'fillStyle');
    Object.defineProperty(proto, 'fillStyle', {
        configurable: true,
        get: own.get,
        set: function (value) {
            if (this.canvas.id === 'board' && window.__paint) {
                window.__paint.fill.push(String(value));
                window.__paint.currentFill = String(value);
            }
            own.set.call(this, value);
        },
    });
    const stroke = Object.getOwnPropertyDescriptor(proto, 'strokeStyle');
    Object.defineProperty(proto, 'strokeStyle', {
        configurable: true,
        get: stroke.get,
        set: function (value) {
            if (this.canvas.id === 'board' && window.__paint) {
                window.__paint.stroke.push(String(value));
                window.__paint.currentStroke = String(value);
            }
            stroke.set.call(this, value);
        },
    });
    const arc = proto.arc;
    proto.arc = function (x, y, radius, start, end, anticlockwise) {
        if (this.canvas.id === 'board' && window.__paint) {
            window.__paint.arc.push({start, end, anticlockwise: Boolean(anticlockwise),
                                     stroke: window.__paint.currentStroke});
        }
        return arc.apply(this, arguments);
    };
}"""

#: Candidate text fills: the signed difference view's white pair, the policy/visits ring colours,
#: and the neutral main label used by winrate and score modes.
LABEL_FILLS = ("#ffffff", "rgba(255, 255, 255, 0.85)", "hsl(8, 80%, 34%)",
               "hsl(218, 72%, 34%)", "#27313a")


def label_text(g: Gowui) -> list[str]:
    """What the last draw wrote on the candidate circles, in the order it wrote it."""
    return [text for text, fill in g.page.evaluate("() => window.__paint.text")
            if fill in LABEL_FILLS]


def test_candidate_arcs_encode_policy_and_share_of_root_visits(start_app, open_page):
    """The two half-rings start at six o'clock: policy climbs left and visits/root climbs right."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    g.page.locator("#label-mode").select_option("prior")
    g.page.evaluate(RECORDER)
    size = g.state()["game"]["size"]
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, [move_info(vertex(0, 0, size), visits=20, prior=0.25)], visits=100)))
    g.expect_dataset("candidates", "1")

    arcs = g.page.evaluate("() => window.__paint.arc")
    policy = next(a for a in arcs if a["stroke"] == "hsla(8, 88%, 48%, 0.98)")
    visits = next(a for a in arcs if a["stroke"] == "hsla(218, 86%, 52%, 0.98)")
    assert policy["start"] == pytest.approx(math.pi / 2)
    assert policy["end"] - policy["start"] == pytest.approx(math.pi * 0.25)
    assert policy["anticlockwise"] is False
    assert visits["start"] == pytest.approx(math.pi / 2)
    assert visits["start"] - visits["end"] == pytest.approx(math.pi * 0.20)
    assert visits["anticlockwise"] is True


def test_a_candidate_the_other_tuple_never_searched_is_drawn_whole(start_app, open_page):
    """§3.8 "Top candidates": a count some candidates carry and others do not is a case, not an
    edge — while comparing, B's candidates are its own moves, and a move A's search never reached
    has no count while its neighbours do.

    Three guards hold the ring for that move together, and none of them is reachable through the
    DOM: the main label is the `-` of "Label modes" and not the word `undefined`; there is no
    second line, rather than a second line reading `undefined`; and there is no blue visits arc.
    """
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    size = g.state()["game"]["size"]
    known, only_b = vertex(0, 0, size), vertex(5, 5, size)
    a = [0.0] * (size * size + 1)
    b = [0.0] * (size * size + 1)
    a[0], b[0] = 0.9, 0.4
    b[5 * size + 5] = 0.6
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, [move_info(known, visits=1000, prior=0.9)], source="handol", policy=a,
        # B names a second move, and the search that ran on the position never evaluated it: the
        # merge leaves its visits undefined, where its neighbour's is a thousand.
        compare={"policy": b, "moveInfos": [move_info(known, visits=None, prior=0.4),
                                            move_info(only_b, visits=None, prior=0.6)]})))
    expect(g.page.locator("table.candidates thead th")).to_have_count(8, timeout=QUICK)
    g.page.locator("#label-mode").select_option("visits")

    g.page.evaluate(RECORDER)
    before = g.draws()
    show_view(g, "B")
    after_a_draw(g, before)
    g.expect_dataset("candidates", "2")

    # The known move's main line and its second line, then the one B alone names — which takes the
    # dash and no second line at all.
    assert label_text(g) == ["1.0k", "1.0k", "-"], (
        f"the circles wrote {label_text(g)}, not the known move's count twice and a dash for the "
        f"move {only_b} that A's search never reached")
    strokes = g.page.evaluate("() => window.__paint.stroke")
    assert strokes.count("hsla(8, 88%, 48%, 0.98)") == 2
    assert strokes.count("hsla(218, 86%, 52%, 0.98)") == 1


# -- a page with no engine claims nothing (#66; §3.2, §3.7, §3.8) ---------------------------------
#: Past the 8 s a status reporting an event lives (§3.8 "Status line"), with margin.
PAST_THE_TIMER = 9_000


def free_port() -> int:
    """A local port nothing listens on (bound, then released)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def status_shown(g: Gowui) -> tuple[str, bool]:
    """The status line's text and whether it is drawn in the error colour (``--bad``), read from
    the computed colour rather than a class name, which is the page's own business."""
    return tuple(g.page.evaluate("""() => {
        const node = document.getElementById('status');
        const probe = document.createElement('span');
        probe.style.color = 'var(--bad)';
        document.body.appendChild(probe);
        const bad = getComputedStyle(probe).color;
        probe.remove();
        return [node.textContent, getComputedStyle(node).color === bad];
    }"""))


def status_frame(g: Gowui, since: int, prefix: str, *, timeout: int = ENGINE) -> dict:
    """The first ``state`` received after ``since`` whose status starts with ``prefix``."""
    g.until("([since, prefix]) => window.__gowuiTest.frames.some((f) => f.seq > since"
            " && f.dir === 'received' && (() => { try { const m = JSON.parse(f.data);"
            " return m.type === 'state' && (m.status || '').startsWith(prefix); }"
            " catch (e) { return false; } })())", [since, prefix], timeout=timeout)
    return next(s for s in g.received(since, "state") if s["status"].startswith(prefix))


def engine_page(start_engine, start_app, open_page, protocol: str = "gtp"):
    """A page on an app connected at startup to a fake engine; the engine too, to kill."""
    engine = start_engine(protocol)
    g = open_page(start_app(engine, True)).open()
    expect(g.page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    return g, engine


def lose_the_engine(g: Gowui, engine, how: str) -> None:
    """``disconnect``: the user's own Disconnect; ``kill``: the engine process dies under it."""
    since = g.mark()
    if how == "disconnect":
        g.page.locator("#connect").click()
    else:
        engine.process.stop()
    g.until("(since) => window.__gowuiTest.frames.some((f) => f.seq > since"
            " && f.dir === 'received' && f.data.startsWith('{\"type\": \"state\"')"
            " && JSON.parse(f.data).engine.connected === false)", since, timeout=ENGINE)
    expect(g.page.locator("#engine-state")).to_have_class(re.compile(r"\boff\b"), timeout=QUICK)
    # One more state from the server, built from the slot's stored analysis (§4.2): a clear the
    # page made on its own would be undone here.
    g.fence()


# The three engine failures are red and stand past the timer; the user's own Disconnect is not red.
def test_a_refused_connect_is_shown_red_and_outlasts_the_timer(start_app, open_page):
    """The server answers a refused connect with an ``error`` and a ``state`` carrying the same
    text 1 ms apart (§3.8): the second must not repaint the first in the muted colour, and neither
    may clear on the 8 s timer, since the engine is still not there."""
    unreachable = types.SimpleNamespace(protocol="gtp", port=free_port())
    g = open_page(start_app(unreachable)).open()
    since = g.mark()
    g.page.locator("#connect").click()
    text = status_frame(g, since, "Engine connection failed")["status"]
    g.page.wait_for_timeout(PAST_THE_TIMER)
    assert status_shown(g) == (text, True)


def test_an_engine_that_dies_is_shown_red_and_outlasts_the_timer(start_engine, start_app,
                                                                 open_page):
    g, engine = engine_page(start_engine, start_app, open_page)
    since = g.mark()
    engine.process.stop()
    text = status_frame(g, since, "Engine disconnected:")["status"]
    g.page.wait_for_timeout(PAST_THE_TIMER)
    assert status_shown(g) == (text, True)


def test_a_restore_whose_engine_is_refused_is_shown_red_and_outlasts_the_timer(
        start_app, open_page, tmp_path):
    """A stored request the policy refuses on restore (§8.1): the local policy takes gtp, analysis
    and handol only, so a snapshot naming another protocol is refused when it is replayed."""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "version": 1, "activeBoard": 1,
        "boards": [{"id": 1, "name": "study", "sgf": "(;GM[1]FF[4]SZ[9])", "cursor": 0}],
        "engine": {"connected": True,
                   "request": {"protocol": "smoke-signals", "host": "127.0.0.1", "port": 1}},
        "play": {}}), encoding="utf-8")
    g = open_page(start_app(None, False, "--state", str(state), fresh=False)).open()
    text = status_frame(g, 0, "Engine not reconnected")["status"]
    g.page.wait_for_timeout(PAST_THE_TIMER)
    assert status_shown(g) == (text, True)


def test_the_users_own_disconnect_is_not_shown_red(start_engine, start_app, open_page):
    """§3.8: a Disconnect reports what the user just asked for. Its text differs from the lost
    engine's only by the missing reason, so a page that coloured by text would get this wrong."""
    g, _ = engine_page(start_engine, start_app, open_page)
    since = g.mark()
    g.page.locator("#connect").click()
    status_frame(g, since, "Engine disconnected")
    g.fence()
    assert status_shown(g) == ("Engine disconnected", False)


# Losing the engine clears everything attributable to a search (§3.8 "Evaluation", "Board strip").
def side_panel_and_board(g: Gowui) -> tuple:
    """Candidate rows, the canvas's candidate record, whether the readout shows any figure, and
    the winrate label."""
    readout = g.page.locator("#candidate-readout").text_content() or ""
    return (g.page.locator("#candidates tr").count(), g.dataset("candidates"),
            bool(re.search(r"\d", readout)), g.page.locator("#winbar-label").text_content())


def tile_figures(g: Gowui, board_id: int) -> tuple[bool, int]:
    """Whether the tile's move line carries a percentage, and how many of its canvas pixels are
    heatmap-coloured: the heatmap is violet over the wood, the only paint on a tile whose blue
    channel is well above its red (grid, stones and the last-move dot are not)."""
    tile = g.page.locator(f'#board-list .thumb[data-id="{board_id}"]')
    meta = tile.locator(".thumb-meta:not(.thumb-tuple)").text_content() or ""
    heat = g.page.evaluate("""(id) => {
        const canvas = document.querySelector(`#board-list .thumb[data-id="${id}"] canvas`);
        const data = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
        let count = 0;
        for (let i = 0; i < data.length; i += 4) if (data[i + 2] > data[i] + 20) count += 1;
        return count;
    }""", board_id)
    return "%" in meta, heat


def analysing(start_engine, start_app, open_page) -> tuple:
    """Connected to the analysis-protocol fake (its answers carry the policy a heatmap needs,
    §2.4) with analysis on and every figure up."""
    g, engine = engine_page(start_engine, start_app, open_page, "analysis")
    g.page.locator("#analysis-on").check()
    g.expect_dataset("candidates", re.compile(r"^[1-9]\d*$"), timeout=ENGINE)
    expect(g.page.locator("#candidates tr").first).to_be_visible(timeout=ENGINE)
    board = g.state()["activeBoard"]
    g.until("(id) => /%/.test(document.querySelector("
            "`#board-list .thumb[data-id=\"${id}\"] .thumb-meta`).textContent)", board,
            timeout=ENGINE)
    assert tile_figures(g, board)[1] > 0, "setup: the tile draws a heatmap while analysing"
    return g, engine, board


@pytest.mark.parametrize("how", ["disconnect", "kill"])
def test_losing_the_engine_clears_the_analysis_shown(start_engine, start_app, open_page, how):
    g, engine, _ = analysing(start_engine, start_app, open_page)
    lose_the_engine(g, engine, how)
    assert side_panel_and_board(g) == (0, "0", False, g.t("noEngine"))


@pytest.mark.parametrize("how", ["disconnect", "kill"])
def test_losing_the_engine_clears_the_active_tiles_winrate_and_heatmap(start_engine, start_app,
                                                                       open_page, how):
    """The tile has a server half: its figures are re-sent from the slot's stored analysis on
    every ``state`` (§4.2), and ``lose_the_engine`` ends on one, so a page-only clear fails."""
    g, engine, board = analysing(start_engine, start_app, open_page)
    lose_the_engine(g, engine, how)
    assert tile_figures(g, board) == (False, 0)


def test_losing_the_engine_clears_a_board_left_behinds_tile(start_engine, start_app, open_page):
    """An inactive tile's figures are the page's cached copy: no ``state`` names them again
    (§4.2), so only the page can drop them."""
    g, engine, board = analysing(start_engine, start_app, open_page)
    g.page.locator("#board-new").click()
    g.until("(id) => window.__gowuiTest && document.querySelector("
            "`#board-list .thumb.active`).dataset.id !== String(id)", board)
    assert tile_figures(g, board)[0], "setup: the board left behind shows its winrate"
    lose_the_engine(g, engine, "disconnect")
    assert tile_figures(g, board) == (False, 0)


# With no root winrate the bar draws no position (§3.8 "Evaluation").
def bar_tones(g: Gowui) -> tuple[bool, bool, bool]:
    """Samples the rendered bar near both ends, clear of the centred label, and reports whether
    it is one tone, and whether that tone is neither stone's colour. A width of 50% fails the
    first (black then white), 0% the third (all white: "White 100%"), 100% the second."""
    g.page.wait_for_timeout(400)   # the black part's width has a 0.2 s transition
    png = base64.b64encode(g.page.locator(".winbar").screenshot(animations="disabled")).decode()
    left, right, black, white = g.page.evaluate("""async (b64) => {
        const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
        const bitmap = await createImageBitmap(new Blob([bytes], { type: 'image/png' }));
        const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
        const ctx = canvas.getContext('2d');
        ctx.drawImage(bitmap, 0, 0);
        const at = (fx) => Array.from(ctx.getImageData(Math.round(bitmap.width * fx),
                                                       Math.round(bitmap.height * 0.3), 1, 1)
                                      .data.slice(0, 3));
        const tone = (name) => {
          const probe = document.createElement('span');
          probe.style.color = `var(${name})`;
          document.body.appendChild(probe);
          const rgb = getComputedStyle(probe).color.match(/\\d+/g).slice(0, 3).map(Number);
          probe.remove();
          return rgb;
        };
        return [at(0.08), at(0.92), tone('--black-stone'), tone('--white-stone')];
    }""", png)

    def distance(a, b):
        return sum(abs(x - y) for x, y in zip(a, b))

    return (distance(left, right) <= 12, distance(left, black) > 60, distance(left, white) > 60)


def test_the_bar_draws_no_position_without_an_engine(start_app, open_page):
    g = open_page(start_app()).open()
    assert bar_tones(g) == (True, True, True)


def test_the_bar_draws_no_position_connected_without_an_analysis(start_engine, start_app,
                                                                 open_page):
    g, _ = engine_page(start_engine, start_app, open_page)
    assert bar_tones(g) == (True, True, True)


def test_the_visit_count_without_an_analysis_is_unknown_not_zero(start_app, open_page):
    g = open_page(start_app()).open()
    text = g.page.locator("#visit-count").text_content() or ""
    assert ("--" in text, bool(re.search(r"\d", text))) == (True, False), text


# The controls that start engine work are cleared on loss and inert without an engine (§3.2, §3.8
# "Capability gating").
PLAY_BOXES = (("#analysis-on", "analysisEnabled"), ("#black-engine", "blackIsEngine"),
              ("#white-engine", "whiteIsEngine"))


def test_an_engine_that_dies_leaves_no_play_setting_ticked(start_engine, start_app, open_page):
    """The page and the last ``state`` agree, and both say off."""
    g, engine = engine_page(start_engine, start_app, open_page)
    g.page.locator("#analysis-on").check()
    g.page.locator("#white-engine").check()   # Black is to move, so the engine waits
    g.until("() => { const f = window.__gowuiTest.frames.filter((f) => f.dir === 'received'"
            " && f.data.startsWith('{\"type\": \"state\"')).pop();"
            " const s = f && JSON.parse(f.data).settings;"
            " return s && s.analysisEnabled && s.whiteIsEngine; }", timeout=ENGINE)
    lose_the_engine(g, engine, "kill")
    settings = g.state()["settings"]
    shown = [g.page.locator(box).is_checked() for box, _ in PLAY_BOXES]
    assert shown + [settings[key] for _, key in PLAY_BOXES] == [False] * 6


@pytest.mark.parametrize("box", [box for box, _ in PLAY_BOXES])
def test_a_play_setting_cannot_be_armed_without_an_engine(start_app, open_page, box):
    g = open_page(start_app()).open()
    since = g.mark()
    g.page.locator(box).click(force=True)
    g.page.wait_for_timeout(300)
    g.fence()
    sent = g.sent(since, "analysis") + g.sent(since, "players")
    assert (sent, g.page.locator(box).is_checked()) == ([], False)


def test_a_player_clicked_while_disconnected_plays_nothing_on_connect(start_engine, start_app,
                                                                     open_page):
    """The case the plan measured: "KataGo plays Black" clicked with no engine, then Connect. The
    engine must not open with a move nobody asked for at that moment."""
    engine = start_engine("gtp")
    g = open_page(start_app(engine, False)).open()
    g.page.locator("#black-engine").click(force=True)
    g.page.wait_for_timeout(300)
    g.page.locator("#connect").click()
    expect(g.page.locator("#engine-state")).to_have_class(re.compile(r"\bon\b"), timeout=ENGINE)
    g.page.wait_for_timeout(1_500)
    g.fence()
    assert g.state()["game"]["moveCount"] == 0


def test_a_control_that_only_configures_stays_live_without_an_engine(start_app, open_page):
    """The other half of the criterion: the server keeps a setting and hands it to the engine that
    arrives, so these are not disabled with the three above."""
    g = open_page(start_app()).open()
    live = ["#max-visits", "#interval", "#show-ownership", "#eval-visits"]
    assert [g.page.locator(field).is_disabled() for field in live] == [False] * len(live)


# An empty catalog says so beside the picker (§3.8 "Engine picker").
def sign_in(open_page, app, name: str, password: str) -> Gowui:
    g = open_page(app)
    g.page.goto(app.url + "/")
    expect(g.page).to_have_url(re.compile(r"/login$"))
    g.page.locator("input[name=name]").fill(name)
    g.page.locator("input[name=password]").fill(password)
    g.page.locator("button[type=submit]").click()
    return g.ready()


def beside_the_picker(g: Gowui) -> list[list[str]]:
    """``[key, text]`` of every visible translated text beside the picker (in its row), other than
    the Connect button and the engine badge, which are there whatever the catalog."""
    return g.page.evaluate("""() => {
        const row = document.getElementById('engine-pick').parentElement;
        return Array.from(row.querySelectorAll('[data-i18n]'))
          .filter((n) => n.id !== 'connect' && n.id !== 'engine-state')
          .filter((n) => n.getClientRects().length > 0 && n.textContent.trim() !== '')
          .map((n) => [n.getAttribute('data-i18n'), n.textContent.trim()]);
    }""")


def test_an_empty_catalog_says_so_beside_the_picker(start_server_app, open_page):
    g = sign_in(open_page, start_server_app(engines=[], users={"alice": "password one"}),
                "alice", "password one")
    shown = beside_the_picker(g)
    assert [(text == g.t(key)) for key, text in shown] == [True], shown


def test_the_empty_catalog_sentence_follows_a_language_change(start_server_app, open_page):
    g = sign_in(open_page, start_server_app(engines=[], users={"alice": "password one"}),
                "alice", "password one")
    english = beside_the_picker(g)
    g.page.locator("#lang").select_option("ko")
    expect(g.page.locator("html")).to_have_attribute("lang", "ko")
    korean = beside_the_picker(g)
    assert (len(english), [(text == g.t(key)) for key, text in korean],
            korean != english) == (1, [True], True), (english, korean)
