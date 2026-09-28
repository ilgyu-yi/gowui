"""The help panel (SPEC §3.8 "Help panel"; §3.7; issue #64).

§3.8 gives the panel a control in the top bar that "opens the panel and closes it again", a close
button of its own, one other opener — the "How to start" button of an empty candidate readout — and
"nothing else opens or closes it (§3.7 leaves it no key)". Its key list is
"**derived, not authored**: one row per entry of the table of §3.7, rendered from that table", and
opening it "moves focus to its close button; closing it returns focus to the control that opened
it".

The surface these tests drive, which is the panel's whole contract with a test:

* ``#help-toggle`` — the top-bar control;
* ``#help-panel`` — the panel, open and closed by the ``hidden`` attribute (§3.8: "not a style
  write", so the §7.5 census is unchanged);
* ``#help-close`` — its close button;
* ``#help-keys`` — the container the key list is rendered into: one element per table entry
  carrying ``data-scope``, one ``<kbd data-key="…">`` per key of that entry (§3.8: "its key rows
  wrap and each key is its own token"), and a ``data-i18n`` label.

Nothing here names a key or a string: the rows are compared against ``window.keyHelp``, which is
the same table the panel renders from, and every label is checked only for **not** reading as its
own key — ``i18n.t`` falls back to the key, so a row reading ``help.key.prev`` is exactly what a
missing string looks like on the page (§3.8 "Language").
"""

from __future__ import annotations

import pytest

from browser_kit import QUICK, Gowui, analysis_frame, analysis_payload, expect, \
    move_info, vertex

pytestmark = pytest.mark.browser


def control(g: Gowui):
    node = g.page.locator("#help-toggle")
    assert node.count() == 1, "the top bar has no #help-toggle (SPEC §3.8 'Help panel')"
    return node


def panel(g: Gowui):
    node = g.page.locator("#help-panel")
    assert node.count() == 1, "the page has no #help-panel (SPEC §3.8 'Help panel')"
    return node


def opened(g: Gowui) -> Gowui:
    """Open the panel through the control §3.8 gives it, and wait until it is up."""
    control(g).click()
    expect(panel(g)).to_be_visible(timeout=QUICK)
    return g


def key_table(g: Gowui) -> list[dict]:
    """``window.keyHelp`` — the table of §3.7 the panel renders from."""
    table = g.page.evaluate("() => window.keyHelp || null")
    assert table, ("the page exports no window.keyHelp, so the key list cannot be derived from "
                   "the table (SPEC §3.7 'one table'; §3.8 'Help panel')")
    return table


def declared_rows(g: Gowui) -> list[list]:
    """``[scope, [key, ...]]`` per entry of the exported table, in its own order."""
    return [[scope["scope"], list(entry["press"])]
            for scope in key_table(g) for entry in scope["keys"]]


def rendered_rows(g: Gowui) -> list[list]:
    """``[scope, [key, ...]]`` per row the panel rendered, in page order."""
    return g.page.evaluate("""() => [...document.querySelectorAll('#help-keys [data-scope]')]
        .map((row) => [row.dataset.scope,
                       [...row.querySelectorAll('kbd[data-key]')].map((k) => k.dataset.key)])""")


def row_labels(g: Gowui) -> list[list[str]]:
    """``[i18n key, shown text]`` of every key row's label."""
    return g.page.evaluate("""() => [...document.querySelectorAll('#help-keys [data-scope]')]
        .map((row) => {
          const label = row.querySelector('[data-i18n]');
          return [label ? label.dataset.i18n : '', label ? label.textContent.trim() : ''];
        })""")


def prose(g: Gowui) -> list[str]:
    """The panel's own written text — everything but the generated key rows."""
    return g.page.evaluate("""() => [...document.querySelectorAll('#help-panel [data-i18n]')]
        .filter((node) => !node.closest('#help-keys'))
        .map((node) => node.textContent.trim())""")


def switched_to(g: Gowui, lang: str) -> None:
    g.page.locator("#lang").select_option(lang)
    expect(g.page.locator("html")).to_have_attribute("lang", lang, timeout=QUICK)


# -- opening and closing (§3.8 "Help panel") -----------------------------------------------------
def test_the_page_starts_with_the_panel_closed(start_app, open_page):
    """§3.8: the panel is opened by its control, so it is not up before anyone asks — and closed
    is the ``hidden`` attribute, not a style write, which is what leaves the §7.5 style-write
    census unchanged."""
    g = open_page(start_app()).open()
    assert panel(g).get_attribute("hidden") is not None, \
        "the panel is on screen before its control was touched, or it is hidden by a style write"


def test_the_top_bar_control_opens_the_panel(start_app, open_page):
    """§3.8: "A control in the top bar opens the panel"."""
    g = open_page(start_app()).open()
    control(g).click()
    expect(panel(g)).to_be_visible(timeout=QUICK)


def test_the_top_bar_control_closes_the_panel_again(start_app, open_page):
    """§3.8: the same control "closes it again" — the one control is the whole switch."""
    g = opened(open_page(start_app()).open())
    control(g).click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)


def test_the_panels_own_close_button_closes_it(start_app, open_page):
    """§3.8: "its own close button closes it too"."""
    g = opened(open_page(start_app()).open())
    g.page.locator("#help-close").click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)


def test_opening_the_panel_moves_focus_to_its_close_button(start_app, open_page):
    """§3.8: "Opening it moves focus to its close button". The panel is not modal, so nothing else
    keeps the keyboard inside it; this is what a keyboard user gets instead."""
    g = opened(open_page(start_app()).open())
    assert g.page.evaluate("() => document.activeElement.id") == "help-close", \
        "opening the panel left focus where it was, so a keyboard user is not in it"


def test_closing_the_panel_returns_focus_to_the_control(start_app, open_page):
    """§3.8: "closing it returns focus to the control that opened it" — not to the document, where
    the next Tab starts again from the top of the page."""
    g = opened(open_page(start_app()).open())
    g.page.locator("#help-close").click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)
    assert g.page.evaluate("() => document.activeElement.id") == "help-toggle", \
        "closing the panel dropped focus instead of giving it back to the control"


# -- the key list is derived from the table (§3.8 "Help panel") ----------------------------------
def test_the_key_list_is_one_row_per_entry_of_the_exported_table(start_app, open_page):
    """§3.8: "one row per entry of the table of §3.7, rendered from that table". Both sides come
    out of the shipped page — the rendered rows and ``window.keyHelp`` — so this file authors no
    key name and a key the table gains appears here without being written down twice."""
    g = opened(open_page(start_app()).open())
    assert rendered_rows(g) == declared_rows(g), \
        "the panel's key rows are not the table's entries, so the list is authored, not derived"


def test_every_scope_of_the_table_renders(start_app, open_page):
    """§3.7: the table declares four surfaces, and the tile contributes two groups. A scope that
    renders no row is a surface the reader never learns the page has."""
    g = opened(open_page(start_app()).open())
    assert sorted({scope for scope, _ in rendered_rows(g)}) == \
        sorted({scope["scope"] for scope in key_table(g)}), \
        "the panel renders rows for some scopes of the table and not others"


def test_the_exported_table_carries_every_key_group_the_source_declares(start_app, open_page):
    """The one hole the two comparisons above share: both sides of them come out of
    ``window.keyHelp``, so a key group ``app.js`` declares and then leaves out of the export
    renders no row, and neither the panel nor the static census notices — the census reads the
    declarations, the panel reads the export, and nothing reads them against each other.

    This does. Both sides are still extracted, one from the shipped source and one from the page
    it built, so no key name is written here either."""
    from test_frontend_keys import table_presses

    g = opened(open_page(start_app()).open())
    assert sorted(tuple(keys) for _, keys in declared_rows(g)) == \
        sorted(tuple(keys) for keys in table_presses()), \
        "app.js declares key groups the exported table leaves out, so the panel cannot list them"


def test_the_key_list_is_not_empty(start_app, open_page):
    """Floor: every comparison above is a comparison of two lists that come from the page, and two
    empty lists are equal. The table has to hold something for any of it to mean anything."""
    g = opened(open_page(start_app()).open())
    assert len(declared_rows(g)) >= len(key_table(g)) > 0, \
        f"the exported table holds {len(declared_rows(g))} entries in {len(key_table(g))} scopes"


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_key_row_reads_as_text_rather_than_as_its_own_key(start_app, open_page, lang):
    """§3.8 "Language" and issue #64 AC 5: every new string is in **both** tables. ``i18n.t``
    falls back to the key itself, so a label missing from a table throws nothing and logs nothing
    — it renders the row as ``help.key.prev``. Equality with its own key is the only signature it
    leaves, in either language."""
    g = open_page(start_app()).open()
    switched_to(g, lang)
    opened(g)
    untranslated = [key for key, text in row_labels(g) if not text or text == key]
    assert untranslated == [], \
        f"{len(untranslated)} key rows show their own key in {lang}: {untranslated}"


# -- a language switch while the panel is open (§3.8 "Language"; issue #64 AC 5) -----------------
def test_switching_language_with_the_panel_open_re_renders_its_prose(start_app, open_page):
    """Issue #64 AC 5: "switching language while the panel is open re-renders it". The written
    half — the flow and the legend — is ``data-i18n`` markup in the page, which ``i18n.apply``
    walks."""
    g = opened(open_page(start_app()).open())
    english = prose(g)
    assert english, "the panel carries no written text, so nothing here measures a re-render"

    switched_to(g, "ko")
    assert prose(g) != english, "the panel's prose is the same text after the language changed"


def test_switching_language_with_the_panel_open_re_renders_the_key_rows(start_app, open_page):
    """The generated half. The rows are built by the panel rather than written into the page, so
    they are the half a re-render can miss: the prose above would still change while every key row
    kept its English label."""
    g = opened(open_page(start_app()).open())
    english = [text for _, text in row_labels(g)]
    assert english, "the panel rendered no key rows, so nothing here measures a re-render"

    switched_to(g, "ko")
    assert [text for _, text in row_labels(g)] != english, \
        "the key rows kept their old text after the language changed"


# -- the first step follows the data (§3.8 "Help panel" part 1; issue #67 AC 6) -------------------
#: The three engine-address shapes §5 can send, as ``/api/health``'s ``engineAddress``; ``None``
#: leaves the local app's own (typed) answer alone.
ADDRESSES = {
    "typed": None,
    "catalog": {"kind": "catalog",
                "engines": [{"id": "kata", "label": "Fake KataGo", "protocol": "gtp"}]},
    "empty catalog": {"kind": "catalog", "engines": []},
}


def with_engine_address(g: Gowui, address: dict) -> None:
    """Answer ``/api/health`` with the app's own body and ``engineAddress`` replaced: the page is
    told a catalog exists by the data alone, while running in local mode — which is what "never a
    mode name" means."""
    def handle(route):
        response = route.fetch()
        body = response.json()
        body["engineAddress"] = address
        route.fulfill(response=response, json=body)

    g.page.route("**/api/health", handle)


def first_step(g: Gowui) -> list[str]:
    """``[i18n key, shown text]`` of the flow's first step."""
    return g.page.evaluate("""() => {
        const step = document.querySelector('#help-panel ol li');
        return step ? [step.dataset.i18n || '', step.textContent.trim()] : ['', ''];
    }""")


def test_the_first_step_follows_the_engine_address(start_app, open_page):
    """§3.8: the first step "is keyed on `engineAddress.kind` from `/api/health`, never on a mode
    name", with a variant for a typed address, a catalog and an empty catalog, and a language
    switch re-renders the variant in force. Three pages, one app: only the data differs."""
    app = start_app()
    seen = {}
    for name, address in ADDRESSES.items():
        g = open_page(app)
        if address is not None:
            with_engine_address(g, address)
        g.open()
        if address is not None:
            # The catalog has been applied once the picker, or the empty note, is up.
            ready = "#engine-pick" if address["engines"] else "#engine-empty"
            expect(g.page.locator(ready)).to_be_visible(timeout=QUICK)
        opened(g)
        english = first_step(g)
        switched_to(g, "ko")
        korean = first_step(g)
        seen[name] = (english, korean)

    untranslated = [(name, key) for name, pair in seen.items() for key, text in pair
                    if not text or text == key]
    assert untranslated == [], f"a first step reads as its own key: {untranslated}"
    stale = [name for name, (en, ko) in seen.items() if en[1] == ko[1]]
    assert stale == [], f"the first step kept its text across a language switch: {stale}"
    for lang in (0, 1):
        texts = [pair[lang][1] for pair in seen.values()]
        assert len(set(texts)) == len(ADDRESSES), \
            f"the first step reads the same for different engine addresses: {dict(zip(seen, texts))}"


# -- the panel is reachable without knowing it exists (§3.8 "Help panel"; issue #67 AC 7) ---------
def test_the_help_control_outweighs_a_plain_button_and_stays_below_connect(start_app, open_page):
    """§3.8: the top-bar control "is drawn in the accent colour, as an outline and its text, so it
    is second in weight to Connect, which stays the only button in the top bar filled with it". The
    plain button is one made for the measurement, in the top bar, and removed again."""
    g = open_page(start_app()).open()
    g.page.mouse.move(1, 1)
    look = g.page.evaluate("""() => {
        const pick = (node) => { const c = getComputedStyle(node);
            return {fill: c.backgroundColor, edge: c.borderTopColor, ink: c.color}; };
        const bar = document.querySelector('.topbar');
        const plain = document.createElement('button');
        plain.type = 'button';
        plain.textContent = 'x';
        bar.appendChild(plain);
        const out = {help: pick(document.getElementById('help-toggle')),
                     connect: pick(document.getElementById('connect')), plain: pick(plain)};
        plain.remove();
        out.filled = [...bar.querySelectorAll('button')]
            .filter((b) => b.getClientRects().length && b.id !== 'connect'
                    && getComputedStyle(b).backgroundColor === out.connect.fill)
            .map((b) => b.id || b.textContent.trim());
        return out;
    }""")
    help_, plain = look["help"], look["plain"]
    assert (help_["edge"], help_["ink"]) != (plain["edge"], plain["ink"]), \
        f"the Help control is drawn like a plain button: {look}"
    assert look["filled"] == [], f"top-bar buttons share Connect's fill: {look['filled']}"


def start_button(g: Gowui):
    return g.page.locator("#candidate-readout button")


def nav_top(g: Gowui) -> float:
    """The navigation row's top in document coordinates."""
    return g.page.evaluate(
        "() => document.querySelector('.board-controls').getBoundingClientRect().top + scrollY")


def with_candidates(g: Gowui) -> None:
    size = g.state()["game"]["size"]
    g.inject(analysis_frame(g.state(), analysis_payload(
        size, [move_info(vertex(x, 0, size)) for x in range(2)])))
    expect(g.page.locator("#candidates tr")).to_have_count(2, timeout=QUICK)


def test_the_empty_readout_offers_the_panel_and_takes_focus_back(start_app, open_page):
    """§3.8 "Candidate readout": with none to show the line "carries a "How to start" button that
    opens the help panel", "at the line's one height"; "Help panel": closing returns focus to
    whichever control opened it. The button goes once a candidate arrives."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    expect(start_button(g)).to_have_count(1, timeout=QUICK)
    label = start_button(g).text_content().strip()
    assert label and not label.startswith("help."), f"the button reads {label!r}"
    empty_top = nav_top(g)

    start_button(g).click()
    expect(panel(g)).to_be_visible(timeout=QUICK)
    assert g.page.evaluate("() => document.activeElement.id") == "help-close"
    g.page.locator("#help-close").click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)
    assert g.page.evaluate("""() => document.activeElement.tagName === 'BUTTON'
        && !!document.activeElement.closest('#candidate-readout')"""), \
        "closing the panel did not give focus back to the readout's button"

    with_candidates(g)
    expect(start_button(g)).to_have_count(0, timeout=QUICK)
    assert nav_top(g) == empty_top, "the navigation row moved as the readout filled"


def test_closing_after_the_readout_button_left_gives_focus_to_the_top_bar_control(start_app,
                                                                                  open_page):
    """§3.8 "Help panel": focus returns "to the top-bar control when that one has left the page —
    the readout's button leaves when a candidate arrives"."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    expect(start_button(g)).to_have_count(1, timeout=QUICK)
    start_button(g).click()
    expect(panel(g)).to_be_visible(timeout=QUICK)
    with_candidates(g)
    expect(start_button(g)).to_have_count(0, timeout=QUICK)
    g.page.locator("#help-close").click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)
    assert g.page.evaluate("() => document.activeElement.id") == "help-toggle", \
        "closing the panel after its opener left dropped focus"


def test_redrawing_a_still_empty_readout_keeps_focus_on_its_button(start_app, open_page):
    """§3.8 "Help panel": closing returns focus to the control that opened it. The readout is
    redrawn on every state and analysis frame; a redraw that leaves the line empty must not drop
    focus from its button to the page body."""
    g = open_page(start_app())
    g.proxy_ws()
    g.open()
    expect(start_button(g)).to_have_count(1, timeout=QUICK)
    start_button(g).click()
    expect(panel(g)).to_be_visible(timeout=QUICK)
    g.page.locator("#help-close").click()
    expect(panel(g)).to_be_hidden(timeout=QUICK)
    size = g.state()["game"]["size"]
    g.inject(analysis_frame(g.state(), analysis_payload(size, [])))
    g.page.wait_for_timeout(200)
    expect(start_button(g)).to_have_count(1, timeout=QUICK)
    assert g.page.evaluate("""() => document.activeElement.tagName === 'BUTTON'
        && !!document.activeElement.closest('#candidate-readout')"""), \
        "redrawing the empty readout dropped focus to " + \
        g.page.evaluate("() => document.activeElement.tagName")
