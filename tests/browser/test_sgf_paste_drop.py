"""Paste and drop an SGF (SPEC §3.8 "SGF"; issue #71).

Both take the Load path: the same route, limits and messages as Load SGF. Each test dispatches a
real ``paste`` event carrying a ``DataTransfer`` with the text, or real ``dragover`` / ``drop``
events carrying a ``DataTransfer`` with ``File`` objects, and reads what the page did from the
requests it made to ``POST /api/sgf``, the move counter and the status line.
"""

from __future__ import annotations

import re

import pytest

from browser_kit import QUICK, Gowui, expect

pytestmark = pytest.mark.browser

#: Three moves, so a load shows ``3 / 3`` on a board that showed ``0 / 0``.
GAME = "(;GM[1]FF[4]SZ[19];B[pd];W[dp];B[pp])"
#: Starts with ``(``, so it is sent, and the reader refuses it (§1.5): a failed load.
BROKEN = "(;GM[1]FF[4]SZ[99])"

PASTE = """([target, text]) => {
  const data = new DataTransfer();
  if (text !== null) data.setData('text/plain', text);
  const event = new ClipboardEvent('paste', { clipboardData: data, bubbles: true,
                                              cancelable: true });
  document.querySelector(target).dispatchEvent(event);
}"""

#: Dispatches ``type`` on ``target`` carrying ``files`` (``[name, text]`` pairs) and, when not
#: null, a plain-text item; answers whether a handler called ``preventDefault``.
DRAG = """([target, type, files, text]) => {
  const data = new DataTransfer();
  files.forEach(([name, body]) => data.items.add(new File([body], name, { type: '' })));
  if (text !== null) data.setData('text/plain', text);
  const event = new DragEvent(type, { dataTransfer: data, bubbles: true, cancelable: true });
  document.querySelector(target).dispatchEvent(event);
  return event.defaultPrevented;
}"""


def open_in(mode: str, start_app, start_server_app, open_page) -> Gowui:
    if mode == "local":
        return open_page(start_app()).open()
    app = start_server_app(users={"alice": "password one"})
    g = open_page(app)
    g.page.goto(app.url + "/")
    g.page.locator("input[name=name]").fill("alice")
    g.page.locator("input[name=password]").fill("password one")
    g.page.locator("button[type=submit]").click()
    return g.ready()


def sgf_posts(g: Gowui) -> list[str]:
    """Every ``POST /api/sgf`` body the page sends from now on, in order."""
    bodies: list[str] = []
    g.page.on("request", lambda r: bodies.append(r.post_data or "")
              if r.method == "POST" and r.url.endswith("/api/sgf") else None)
    return bodies


def paste(g: Gowui, text: str | None, target: str = "body") -> None:
    g.page.evaluate(PASTE, [target, text])


def drag(g: Gowui, type: str, files: list[tuple[str, str]], target: str = "#board-wrap",
         text: str | None = None) -> bool:
    return g.page.evaluate(DRAG, [target, type, [list(f) for f in files], text])


def settle(g: Gowui) -> None:
    """Let a request the page might have started go out before asserting it did not."""
    g.page.wait_for_timeout(500)


def load_through_the_picker(g: Gowui, text: str, tmp_path) -> str:
    """Load SGF with ``text`` as the file; returns the red status it shows."""
    path = tmp_path / "picked.sgf"
    path.write_text(text, encoding="utf-8")
    g.page.locator("#sgf-file").set_input_files(str(path))
    status = g.page.locator("#status")
    expect(status).to_have_class(re.compile(r"\berror\b"), timeout=QUICK)
    expect(status).not_to_have_text("")
    return status.text_content()


# -- paste --------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["local", "server"])
def test_a_pasted_sgf_loads_as_load_sgf_does(mode, start_app, start_server_app, open_page):
    """§3.8 "SGF": a paste whose target is no field posts the text to ``POST /api/sgf``."""
    g = open_in(mode, start_app, start_server_app, open_page)
    posts = sgf_posts(g)
    paste(g, GAME)
    expect(g.page.locator("#move-counter")).to_have_text("3 / 3", timeout=QUICK)
    assert posts == [GAME]


def test_a_pasted_sgf_the_reader_refuses_says_what_load_sgf_says(start_app, open_page,
                                                                  tmp_path):
    """§3.8 "SGF": the same messages as Load SGF, and the board as it was."""
    g = open_page(start_app()).open()
    g.allow_console("400")
    said = load_through_the_picker(g, BROKEN, tmp_path)
    g.page.evaluate("() => { const s = document.getElementById('status');"
                    " s.textContent = ''; s.classList.remove('error'); }")
    posts = sgf_posts(g)
    paste(g, BROKEN)
    status = g.page.locator("#status")
    expect(status).to_have_text(said, timeout=QUICK)
    expect(status).to_have_class(re.compile(r"\berror\b"))
    assert posts == [BROKEN]
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_pasted_text_that_is_not_sgf_is_no_load_attempt(start_app, open_page):
    """§3.8 "SGF": no request, a muted status saying so, the board as it was."""
    g = open_page(start_app()).open()
    posts = sgf_posts(g)
    paste(g, "  just some words")
    status = g.page.locator("#status")
    expect(status).to_have_text(g.t("sgf.notSgf"), timeout=QUICK)
    expect(status).not_to_have_class(re.compile(r"\berror\b"))
    settle(g)
    assert posts == []
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_a_paste_with_no_text_does_nothing(start_app, open_page):
    g = open_page(start_app()).open()
    posts = sgf_posts(g)
    paste(g, None)
    settle(g)
    assert posts == []
    assert g.page.locator("#status").text_content() == ""


@pytest.mark.parametrize("field", ["#raw", "#host", "#protocol", ".thumb-rename"])
def test_a_paste_into_a_field_is_the_fields(field, start_app, open_page):
    """§3.8 "SGF": a paste into an ``input``, ``select`` or the rename field is left alone."""
    g = open_page(start_app()).open()
    if field == ".thumb-rename":
        g.page.locator("#board-list .thumb .thumb-edit").first.click()
    g.page.locator(field).focus()
    posts = sgf_posts(g)
    paste(g, GAME, field)
    settle(g)
    assert posts == []
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


# -- drop ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("mode", ["local", "server"])
def test_a_file_dropped_on_the_board_loads_as_load_sgf_does(mode, start_app, start_server_app,
                                                            open_page):
    g = open_in(mode, start_app, start_server_app, open_page)
    posts = sgf_posts(g)
    assert drag(g, "dragover", [("game.sgf", GAME)]) is True
    assert drag(g, "drop", [("game.sgf", GAME)]) is True
    expect(g.page.locator("#move-counter")).to_have_text("3 / 3", timeout=QUICK)
    assert posts == [GAME]


def test_a_dropped_file_the_reader_refuses_says_what_load_sgf_says(start_app, open_page,
                                                                   tmp_path):
    g = open_page(start_app()).open()
    g.allow_console("400")
    said = load_through_the_picker(g, "not an sgf", tmp_path)
    g.page.evaluate("() => { const s = document.getElementById('status');"
                    " s.textContent = ''; s.classList.remove('error'); }")
    posts = sgf_posts(g)
    drag(g, "drop", [("notes.txt", "not an sgf")])
    status = g.page.locator("#status")
    expect(status).to_have_text(said, timeout=QUICK)
    expect(status).to_have_class(re.compile(r"\berror\b"))
    assert posts == ["not an sgf"]
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


@pytest.mark.parametrize("beside", ["#board-list .thumb", "aside.side"])
def test_a_file_dropped_beside_the_board_loads_nothing_and_keeps_the_page(beside, start_app,
                                                                          open_page):
    """§3.8 "SGF": the board strip and the side panel are outside the drop target, and the
    document cancels a file drag there, so the browser does not open the file in place of the
    page. A cancelled ``dragover`` / ``drop`` is what keeps Chromium from navigating."""
    g = open_page(start_app()).open()
    url = g.page.url
    posts = sgf_posts(g)
    assert drag(g, "dragover", [("game.sgf", GAME)], beside) is True
    assert drag(g, "drop", [("game.sgf", GAME)], beside) is True
    settle(g)
    assert posts == []
    assert g.page.url == url
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


@pytest.mark.parametrize("beside", ["#board-list .thumb", "aside.side"])
def test_a_drag_carrying_no_file_beside_the_board_is_left_alone(beside, start_app, open_page):
    g = open_page(start_app()).open()
    assert drag(g, "dragover", [], beside, text=GAME) is False
    assert drag(g, "drop", [], beside, text=GAME) is False


def test_several_dropped_files_load_none(start_app, open_page):
    g = open_page(start_app()).open()
    posts = sgf_posts(g)
    drag(g, "drop", [("a.sgf", GAME), ("b.sgf", GAME)])
    status = g.page.locator("#status")
    expect(status).to_have_text(g.t("sgf.dropOne"), timeout=QUICK)
    expect(status).to_have_class(re.compile(r"\berror\b"))
    settle(g)
    assert posts == []
    expect(g.page.locator("#move-counter")).to_have_text("0 / 0")


def test_the_board_is_marked_while_files_are_dragged_over_it(start_app, open_page):
    g = open_page(start_app()).open()
    wrap = g.page.locator("#board-wrap")
    drag(g, "dragover", [("game.sgf", GAME)])
    expect(wrap).to_have_class(re.compile(r"\bdrop-ready\b"))
    g.page.evaluate("() => document.getElementById('board-wrap').dispatchEvent("
                    "new DragEvent('dragleave', { bubbles: true, relatedTarget: document.body }))")
    expect(wrap).not_to_have_class(re.compile(r"\bdrop-ready\b"))
    drag(g, "dragover", [("game.sgf", GAME)])
    drag(g, "drop", [("game.sgf", GAME)])
    expect(wrap).not_to_have_class(re.compile(r"\bdrop-ready\b"))


def test_a_drag_carrying_no_file_is_not_taken(start_app, open_page):
    g = open_page(start_app()).open()
    posts = sgf_posts(g)
    assert drag(g, "dragover", [], text=GAME) is False
    expect(g.page.locator("#board-wrap")).not_to_have_class(re.compile(r"\bdrop-ready\b"))
    drag(g, "drop", [], text=GAME)
    settle(g)
    assert posts == []
    assert g.page.locator("#status").text_content() == ""
