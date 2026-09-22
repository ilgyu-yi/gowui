"""Page rules that only a browser can show (SPEC §3.8, §8.5; issue #22).

Each test drives the page's own controls and frames, never its internals: the status line after a
``4403`` close, a rename field while its tile moves, the PV preview of a candidate the board did
not draw, the ring on the stone just played, an empty Visits field, and the presets this browser
has stored.

The frames the server would not send on cue (a reordered ``state``, an ``analysis``) go in through
``proxy_ws`` / ``inject`` of browser_kit.py.
"""

from __future__ import annotations

import json

import pytest

from browser_kit import QUICK, Gowui, analysis_frame, analysis_payload, expect, move_info, vertex

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
#: Wrap `fillText` and the `fillStyle` setter on the 2D context prototype, so a draw's text and
#: colours can be read back. A canvas is otherwise write-only to a test: `-` and `undefined` are
#: both "some white pixels" to a screenshot, and an invalid colour is *no* pixels at all — the
#: assignment is ignored and the shape keeps the previous fill, which is the failure that lies.
#: The assigned value is recorded, not the property afterwards, for exactly that reason.
RECORDER = """() => {
    const proto = CanvasRenderingContext2D.prototype;
    window.__paint = {text: [], fill: []};
    const fillText = proto.fillText;
    proto.fillText = function (text) {
        if (this.canvas.id === 'board' && window.__paint) {
            window.__paint.text.push([String(text), String(this.fillStyle)]);
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
            }
            own.set.call(this, value);
        },
    });
}"""

#: The two fills the candidate labels are painted in: the main line and the second, smaller one.
#: Everything else the board writes — the coordinates — is painted in the wood's brown, so the
#: fill separates the circles' text from the grid's without the test knowing either font.
LABEL_FILLS = ("#ffffff", "rgba(255, 255, 255, 0.85)")


def label_text(g: Gowui) -> list[str]:
    """What the last draw wrote on the candidate circles, in the order it wrote it."""
    return [text for text, fill in g.page.evaluate("() => window.__paint.text")
            if fill in LABEL_FILLS]


def test_a_candidate_the_other_tuple_never_searched_is_drawn_whole(start_app, open_page):
    """§3.8 "Top candidates": a count some candidates carry and others do not is a case, not an
    edge — while comparing, B's candidates are its own moves, and a move A's search never reached
    has no count while its neighbours do.

    Three guards hold the circle for that move together, and none of them is reachable through the
    DOM: the main label is the `-` of "Label modes" and not the word `undefined`; there is no
    second line, rather than a second line reading `undefined`; and the weight is nothing rather
    than a `NaN`, which is not a colour — the browser drops an invalid `fillStyle`, so the circle
    would silently keep the *previous* candidate's fill and say the wrong weight instead of none.
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
    invalid = [fill for fill in g.page.evaluate("() => window.__paint.fill") if "NaN" in fill]
    assert invalid == [], (
        f"the draw asked for {len(invalid)} colours a browser cannot parse: {invalid} — an "
        "ignored fillStyle leaves the circle the previous one's colour, so the shade lies")
