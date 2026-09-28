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

from frontend_helpers import (board_option_controls, i18n_tables, read_static,
                              table_head_keys)

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
        "track", "wbr"}

#: The profile `?` is the one card §3.8 "Where explanation lives" keeps scripted: it follows the
#: profile typed beside it, and issue #67 leaves it as it is.
SCRIPTED_DOT = "profile-help"


class Node:
    def __init__(self, tag: str, attrs: dict[str, str | None], parent: "Node | None"):
        self.tag, self.attrs, self.parent = tag, attrs, parent
        self.children: list[Node] = []

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


@pytest.mark.parametrize("control_id", [*board_option_controls(), "compare-on"])
def test_a_board_switch_carries_a_card_after_its_label(control_id):
    """§3.8 "Where explanation lives" rule 1, with the Compare checkbox the §3.8 table names: the
    `?` is the next element after the control's ``<label>`` (not inside it), and its card, named by
    ``aria-describedby``, is the `?`'s own next element."""
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
