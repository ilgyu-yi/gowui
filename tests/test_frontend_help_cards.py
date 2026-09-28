"""The `?` cards beside the controls (SPEC §3.8 "Where explanation lives"; issue #67).

Every set here is read from the page's own sources, never listed by hand: the controls that change
what the board draws are the ones whose ``onchange`` handler in ``app.js`` calls
``board.setOptions(``, the Label card's lines are checked against the options of ``#label-mode``,
and the table card's entries against the ``*_HEAD`` column sets of ``app.js``. A control added
later that changes the board inherits the rule without an edit here.

``index.html`` is read with the standard library's HTML parser into a small tree, because the
structure is the contract: the `?` sits after its control's ``<label>``, never inside it, and the
card is the `?`'s immediate next element sibling, which is what lets one stylesheet rule
(``.help-dot:focus + .help-card``) open it.
"""

from __future__ import annotations

from html.parser import HTMLParser

import pytest

from frontend_helpers import (board_option_controls, carded_controls, i18n_tables,
                              read_static, table_head_keys)

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}

#: The profile `?` is the one card §3.8 "Where explanation lives" keeps scripted: it follows the
#: profile typed beside it, and issue #67 leaves it as it is.
SCRIPTED_DOT = "profile-help"


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node] = []
        self.text = ""

    @property
    def classes(self) -> list[str]:
        return (self.attrs.get("class") or "").split()

    def walk(self):
        for child in self.children:
            yield child
            yield from child.walk()

    def next_element(self) -> "Node | None":
        siblings = self.parent.children if self.parent else []
        index = siblings.index(self)
        return siblings[index + 1] if index + 1 < len(siblings) else None

    def ancestors(self):
        node = self.parent
        while node is not None:
            yield node
            node = node.parent


class _Tree(HTMLParser):
    def __init__(self):
        super().__init__()
        self.root = Node("#root", {}, None)
        self.open = self.root

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), self.open)
        self.open.children.append(node)
        if tag not in VOID:
            self.open = node

    def handle_data(self, data):
        self.open.text += data

    def handle_endtag(self, tag):
        node = self.open
        while node is not self.root and node.tag != tag:
            node = node.parent
        if node is not self.root:
            self.open = node.parent


def page() -> Node:
    tree = _Tree()
    tree.feed(read_static("index.html"))
    return tree.root


def by_id(root: Node, element_id: str) -> Node | None:
    return next((n for n in root.walk() if n.attrs.get("id") == element_id), None)


def dots(root: Node) -> list[Node]:
    return [n for n in root.walk() if "help-dot" in n.classes]


def dot_after_label(root: Node, control_id: str) -> Node | None:
    """The `?` for ``control_id``: the next element sibling of the ``<label>`` holding it."""
    control = by_id(root, control_id)
    assert control is not None, f"index.html has no #{control_id}"
    label = next((a for a in control.ancestors() if a.tag == "label"), None)
    assert label is not None, f"#{control_id} is not inside a <label>"
    after = label.next_element()
    return after if after is not None and "help-dot" in after.classes else None


def card_of(root: Node, dot: Node) -> Node | None:
    """The card a `?` names with ``aria-describedby``, when it is the `?`'s next element."""
    named = dot.attrs.get("aria-describedby")
    card = by_id(root, named) if named else None
    return card if card is not None and card is dot.next_element() else None


def table_card(root: Node) -> Node | None:
    """The card holding the ``[data-col]`` entries."""
    holders = {id(c): c for n in root.walk() if "data-col" in n.attrs
               for c in n.ancestors() if "help-card" in c.classes}
    assert len(holders) <= 1, "the [data-col] entries are spread over more than one card"
    return next(iter(holders.values()), None)


def cards(root: Node) -> list[Node]:
    return [n for n in root.walk() if "help-card" in n.classes]


# -- which controls carry a card ----------------------------------------------------------------------
def test_the_board_switches_are_read_from_the_source():
    """Count guard for the reader below: the handlers of §3.8 "Controls" that change what the board
    draws are Ownership, the Label select, the heatmap and Move numbers."""
    assert len(board_option_controls()) >= 4


@pytest.mark.parametrize("control_id", carded_controls())
def test_a_carded_control_carries_a_card_after_its_label(control_id):
    """§3.8 "Where explanation lives" rule 1, with the Compare checkbox and the protocol select the
    §3.8 table names: the `?` is the next element after the control's ``<label>`` (not inside it),
    and its card, named by ``aria-describedby``, is the `?`'s own next element."""
    root = page()
    dot = dot_after_label(root, control_id)
    assert dot is not None, f"#{control_id} has no ? right after its <label>"
    assert card_of(root, dot) is not None, (
        f"the ? after #{control_id} names {dot.attrs.get('aria-describedby')!r}, which is not "
        f"its next element")


def test_every_static_dot_names_its_own_card():
    """Every `?` but the scripted profile one names, with ``aria-describedby``, a ``.help-card``
    that is its own next element — the one sibling ``.help-dot:focus + .help-card`` reaches."""
    root = page()
    static = [d for d in dots(root) if d.attrs.get("id") != SCRIPTED_DOT]
    assert static, "the page has no ? beside any control but the profile field"
    broken = [d.attrs.get("aria-describedby") for d in static
              if card_of(root, d) is None or "help-card" not in card_of(root, d).classes]
    assert broken == []


def test_a_dot_is_a_tab_stop():
    root = page()
    static = [d for d in dots(root) if d.attrs.get("id") != SCRIPTED_DOT]
    assert static, "the page has no ? beside any control but the profile field"
    assert [d.attrs.get("aria-describedby") for d in static if d.attrs.get("tabindex") != "0"] \
        == []


def test_a_card_is_not_closed_by_the_hidden_attribute():
    """§3.8: closed is a class rule — ``[hidden]`` is ``display: none !important``, which no
    opening rule can override."""
    found = cards(page())
    assert found, "the page has no .help-card"
    assert [c.attrs.get("id") for c in found if "hidden" in c.attrs] == []


# -- what the cards carry -----------------------------------------------------------------------------
def test_the_label_card_has_a_line_per_label_mode():
    """§3.8 row "Label": one line per mode of the select, marked ``data-option``."""
    root = page()
    select = by_id(root, "label-mode")
    modes = [o.attrs.get("value") for o in select.walk() if o.tag == "option"]
    assert len(modes) >= 4
    dot = dot_after_label(root, "label-mode")
    assert dot is not None and card_of(root, dot) is not None, "the Label select has no card"
    lines = [n.attrs["data-option"] for n in card_of(root, dot).walk() if "data-option" in n.attrs]
    assert sorted(lines) == sorted(modes)


def protocol_options(root: Node) -> list[Node]:
    select = by_id(root, "protocol")
    assert select is not None, "index.html has no #protocol"
    return [o for o in select.walk() if o.tag == "option"]


def test_the_engine_card_has_a_line_per_protocol():
    """§3.8 row "Engine": one line per option of the protocol select, marked ``data-option``."""
    root = page()
    protocols = [o.attrs.get("value") for o in protocol_options(root)]
    assert len(protocols) >= 3
    dot = dot_after_label(root, "protocol")
    assert dot is not None and card_of(root, dot) is not None, "the protocol select has no card"
    lines = [n.attrs["data-option"] for n in card_of(root, dot).walk() if "data-option" in n.attrs]
    assert sorted(lines) == sorted(protocols)


def test_every_protocol_option_takes_its_text_from_the_tables():
    """§3.8 "Engine form": the three options read from the string tables (§3.8 "Language"), so a
    language switch re-renders them; an option written only in ``index.html`` stays English."""
    found = protocol_options(page())
    assert len(found) >= 3
    tables = i18n_tables()
    assert [(o.attrs.get("value"), lang) for o in found for lang in ("en", "ko")
            if o.attrs.get("data-i18n") not in tables[lang]] == []


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_no_protocol_option_reads_as_a_section_heading(lang):
    """§3.8 "Engine form": the ``analysis`` option is renamed rather than defined, so it no longer
    reads the same as the Analysis section's heading. Both texts are what the page shows in
    ``lang``: a key's text from the table, and an option's own text where it has no key."""
    root = page()
    table = i18n_tables()[lang]
    headings = {table.get(n.attrs["data-i18n"], n.text.strip()) for n in root.walk()
                if n.tag == "summary" and "data-i18n" in n.attrs}
    assert len(headings) >= 6, f"the section headings read as {headings}"
    shown = {o.attrs.get("value"): table.get(o.attrs.get("data-i18n") or "", o.text.strip())
             for o in protocol_options(root)}
    assert {value: text for value, text in shown.items() if text in headings} == {}


def test_every_table_column_has_exactly_one_entry():
    """§3.8 row "Candidate table": every column any mode can show has one entry in the table card,
    and no entry names a column no mode shows."""
    heads = table_head_keys()
    assert len(heads) >= 10, f"the *_HEAD arrays read as {heads}"
    card = table_card(page())
    assert card is not None, "the page has no card with [data-col] entries"
    named = [key for n in card.walk() if "data-col" in n.attrs
             for key in n.attrs["data-col"].split()]
    assert (sorted(k for k in heads if named.count(k) != 1), sorted(set(named) - set(heads))) \
        == ([], [])


def test_every_table_entry_has_one_text_for_the_header_title():
    """§3.8: a column header's ``title`` is its entry's text, from the same key — so each entry
    marks exactly one ``.help-text`` carrying a ``data-i18n`` key."""
    card = table_card(page())
    assert card is not None, "the page has no card with [data-col] entries"
    entries = [n for n in card.walk() if "data-col" in n.attrs]
    texts = [[t.attrs.get("data-i18n") for t in e.walk() if "help-text" in t.classes]
             for e in entries]
    assert entries and [len(t) == 1 and bool(t[0]) for t in texts] == [True] * len(entries)


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_every_card_key_is_in_the_table(lang):
    keys = {n.attrs["data-i18n"] for c in cards(page()) for n in c.walk() if "data-i18n" in n.attrs}
    assert keys, "the cards carry no data-i18n text"
    assert sorted(keys - set(i18n_tables()[lang])) == []


# -- the help panel points at the cards (§3.8 "Help panel"; "Where explanation lives" rules 3, 4) ---
def help_panel(root: Node) -> Node:
    panel = by_id(root, "help-panel")
    assert panel is not None, "index.html has no #help-panel"
    return panel


def label_key(root: Node, control_id: str) -> str | None:
    """The ``data-i18n`` key of the text in ``control_id``'s own ``<label>``: the switch's name."""
    control = by_id(root, control_id)
    label = next((a for a in control.ancestors() if a.tag == "label"), None)
    assert label is not None, f"#{control_id} is not inside a <label>"
    keys = [n.attrs["data-i18n"] for n in label.walk()
            if "data-i18n" in n.attrs and n.tag not in ("option", "select")]
    return keys[0] if keys else None


@pytest.mark.parametrize("control_id", [*board_option_controls(), "compare-on"])
def test_the_panel_has_one_task_line_per_switch_naming_it_by_its_own_key(control_id):
    """§3.8 "Help panel" part 3: one line per switch of the `?` table, naming the switch by its own
    label key — the name is reused, never restated (rule 4) — and pointing at its `?` card."""
    root = page()
    lines = [n for n in help_panel(root).walk() if n.attrs.get("data-for") == control_id]
    assert len(lines) == 1, f"the panel has {len(lines)} task lines for #{control_id}"
    key = label_key(root, control_id)
    assert key, f"#{control_id}'s label carries no data-i18n key"
    assert key in [n.attrs.get("data-i18n") for n in lines[0].walk()], \
        f"the task line for #{control_id} does not name it by {key!r}"


def test_every_task_line_is_for_a_switch_that_has_a_card():
    """The other direction: a task line for a control without a `?` points at nothing."""
    root = page()
    switches = {*board_option_controls(), "compare-on"}
    named = [n.attrs["data-for"] for n in help_panel(root).walk() if "data-for" in n.attrs]
    assert named, "the panel has no task line"
    assert sorted(set(named) - switches) == []


def test_the_legend_points_at_the_table_card_rather_than_copying_it():
    """§3.8 "What the numbers mean": for Visits, Policy / Prob. and Value the legend is one pointer
    to the table's `?`, not a second copy of that card. The one entry it keeps is the Win / Score
    one, which issue #67 protects — found as the table entry covering the ``col.win`` column."""
    root = page()
    card = table_card(root)
    assert card is not None, "the page has no card with [data-col] entries"
    entries = [n for n in card.walk() if "data-col" in n.attrs]
    text_key = {tuple(e.attrs["data-col"].split()): t.attrs.get("data-i18n")
                for e in entries for t in e.walk() if "help-text" in t.classes}
    kept = {key for cols, key in text_key.items() if "col.win" in cols}
    assert kept, "no table entry covers col.win"
    shown = {n.attrs["data-i18n"] for n in help_panel(root).walk() if "data-i18n" in n.attrs}
    copied = sorted((set(text_key.values()) & shown) - kept)
    assert copied == [], f"the panel repeats the table card's entries {copied}"
