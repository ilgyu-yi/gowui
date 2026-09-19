"""The browser tests' page driver (SPEC §3.8; tests/browser/conftest.py starts the processes).

Nothing here imports Playwright at module level, so the default run (which deselects the
``browser`` marker) collects these modules without it.

**Frame recorder.** An init script wraps ``window.WebSocket`` before the page's scripts run: every
frame the page puts on an *open* socket and every frame it receives is appended, in order, to
``window.__gowuiTest.frames`` as ``{seq, dir: 'sent' | 'received' | 'close', data | code}``. A
frame the page drops because the socket is not open is never recorded, which is what §3.8
"Connection" requires.

**Fence.** A "nothing was sent" check never sleeps. ``fence()`` sends a ``{type: 'state', fence:
n}`` marker on the page's own socket and waits for the server's ``state`` answer; frames go out
and are handled in order (§4.3), so any frame the page sent before the marker is recorded before
it, and the answer proves the server read everything before it.

The page's ``ack`` frames (§4.3 "Acknowledgement") are recorded too, but ``sent`` and
``wait_sent`` leave them out, as they leave out the fence; ``acks()`` counts them.

**Draw record.** ``dataset(key)`` reads the board canvas's §3.8 "Test observability" record.

**Waiting.** ``until(js)`` polls a page expression from Python, pausing between tries with
Playwright's own ``wait_for_timeout`` (which runs outside the page). Unlike
``page.wait_for_function`` it keeps working while a test holds the page's clock paused
(``page.clock``), where the page's own timers, and so ``wait_for_function``'s polling, stop.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
ARTIFACTS = REPO / "test-artifacts"

#: Assertion timeout (ms) for page state reached without an engine round trip.
QUICK = 5_000
#: Assertion timeout (ms) for page state that waits on the (fake) engine.
ENGINE = 15_000

INIT_SCRIPT = r"""
(() => {
  const record = { frames: [], sockets: [], csp: [], seq: 0, fences: 0, byTest: false };
  Object.defineProperty(window, '__gowuiTest', { value: record });
  document.addEventListener('securitypolicyviolation', (e) => {
    record.csp.push(e.violatedDirective + ' ' + e.blockedURI);
  });
  const Native = window.WebSocket;
  function Recorded(url, protocols) {
    const socket = protocols === undefined ? new Native(url) : new Native(url, protocols);
    record.sockets.push(socket);
    const nativeSend = Native.prototype.send;
    socket.send = function (data) {
      if (socket.readyState === Native.OPEN) {
        record.frames.push({ seq: ++record.seq, dir: 'sent', data: String(data),
                             byTest: record.byTest });
      }
      return nativeSend.call(socket, data);
    };
    socket.addEventListener('message', (e) => {
      record.frames.push({ seq: ++record.seq, dir: 'received', data: String(e.data) });
    });
    socket.addEventListener('close', (e) => {
      record.frames.push({ seq: ++record.seq, dir: 'close', code: e.code });
    });
    return socket;
  }
  Recorded.prototype = Native.prototype;
  ['CONNECTING', 'OPEN', 'CLOSING', 'CLOSED'].forEach((k) => { Recorded[k] = Native[k]; });
  window.WebSocket = Recorded;
})();
"""

_MATCH_JS = r"""
const matches = (frame, dir, type, since, where) => {
  if (frame.seq <= since || frame.dir !== dir || frame.byTest) return false;
  let parsed;
  try { parsed = JSON.parse(frame.data); } catch (err) { return false; }
  if (parsed.fence !== undefined) return false;
  if (dir === 'sent' && parsed.type === 'ack') return false;
  if (type !== null && parsed.type !== type) return false;
  return Object.entries(where || {}).every(([k, v]) => JSON.stringify(parsed[k]) === JSON.stringify(v));
};
"""


def expect(*args, **kwargs):
    """Playwright's ``expect``, imported lazily."""
    from playwright.sync_api import expect as playwright_expect

    return playwright_expect(*args, **kwargs)


def kebab(key: str) -> str:
    return re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), key)


class Gowui:
    """One browser tab on the app, with the recorder installed."""

    def __init__(self, page, url: str):
        self.page = page
        self.url = url
        self.console_errors: list[str] = []
        self.allowed_console: list[re.Pattern] = []
        self.routes: list[Any] = []       # WebSocketRoute objects while proxying (route_ws)
        self.servers: list[Any] = []
        page.on("console", self._on_console)
        page.on("pageerror", lambda error: self.console_errors.append(f"pageerror: {error}"))

    # -- lifecycle ----------------------------------------------------------------------------
    def _on_console(self, message) -> None:
        if message.type == "error":
            self.console_errors.append(message.text)

    def allow_console(self, pattern: str) -> None:
        """Allow console errors matching ``pattern`` (a close the test provokes, for example)."""
        self.allowed_console.append(re.compile(pattern))

    def goto(self, path: str = "/") -> "Gowui":
        self.page.goto(self.url + path)
        return self

    def ready(self) -> "Gowui":
        """Wait until the page has applied its first ``state`` (the move counter shows it)."""
        expect(self.page.locator("#move-counter")).to_have_text(re.compile(r"^\d+ / \d+$"),
                                                                 timeout=QUICK)
        return self

    def open(self, path: str = "/") -> "Gowui":
        return self.goto(path).ready()

    def problems(self) -> list[str]:
        """Console errors (not allowed ones) and CSP violations seen so far."""
        found = [text for text in self.console_errors
                 if not any(p.search(text) for p in self.allowed_console)]
        try:
            csp = self.page.evaluate("() => window.__gowuiTest ? window.__gowuiTest.csp : []")
        except Exception:  # noqa: BLE001 - a closed page has nothing more to report
            csp = []
        return found + [f"CSP violation: {v}" for v in csp]

    def screenshot(self, name: str) -> Path:
        ARTIFACTS.mkdir(exist_ok=True)
        path = ARTIFACTS / f"{name}.png"
        self.page.screenshot(path=str(path), full_page=True)
        return path

    # -- the recorder -----------------------------------------------------------------------------
    def frames(self, since: int = 0) -> list[dict]:
        return self.page.evaluate(
            "(since) => window.__gowuiTest.frames.filter((f) => f.seq > since)", since)

    def mark(self) -> int:
        return self.page.evaluate("() => window.__gowuiTest.seq")

    def _parsed(self, direction: str, since: int) -> list[dict]:
        out = []
        for frame in self.frames(since):
            if frame["dir"] != direction or frame.get("byTest"):
                continue
            parsed = json.loads(frame["data"])
            if isinstance(parsed, dict) and "fence" in parsed:
                continue
            if direction == "sent" and isinstance(parsed, dict) and parsed.get("type") == "ack":
                continue  # the transport's (§4.3 "Acknowledgement"); see ``acks``
            out.append(parsed)
        return out

    def sent(self, since: int = 0, type: str | None = None) -> list[dict]:
        return [f for f in self._parsed("sent", since) if type is None or f.get("type") == type]

    def received(self, since: int = 0, type: str | None = None) -> list[dict]:
        return [f for f in self._parsed("received", since)
                if type is None or f.get("type") == type]

    def acks(self, since: int = 0) -> int:
        """How many ``ack`` frames the page sent after ``since`` (§4.3 "Acknowledgement");
        ``sent`` and ``wait_sent`` leave them out."""
        return self.page.evaluate(
            "(since) => window.__gowuiTest.frames.filter((f) => f.seq > since && f.dir === 'sent'"
            " && /^\\{\"type\": ?\"ack\"\\}$/.test(f.data)).length", since)

    def states_received(self, since: int = 0) -> int:
        """How many ``state`` frames the page received after ``since``, fence answers included."""
        return self.page.evaluate(
            "(since) => window.__gowuiTest.frames.filter((f) => f.seq > since"
            " && f.dir === 'received' && f.data.startsWith('{\"type\": \"state\"')).length",
            since)

    def throttle(self, rate: float) -> None:
        """Slow the page's CPU ``rate`` times (Chromium's DevTools emulation), like a slow
        machine: a slow page is what §4.3 "Acknowledgement" keeps up to date."""
        cdp = self.page.context.new_cdp_session(self.page)
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": rate})

    def closes(self, since: int = 0) -> list[int]:
        return self.page.evaluate(
            "(since) => window.__gowuiTest.frames.filter((f) => f.seq > since"
            " && f.dir === 'close').map((f) => f.code)", since)

    def last_received(self, type: str) -> dict | None:
        """The newest received frame of ``type``, found in the page: a long engine-vs-engine
        run records thousands of frames, too many to copy out on every poll."""
        data = self.page.evaluate("""(type) => {
            const frames = window.__gowuiTest.frames;
            for (let i = frames.length - 1; i >= 0; i -= 1) {
              const f = frames[i];
              if (f.dir !== 'received' || f.byTest) continue;
              let parsed;
              try { parsed = JSON.parse(f.data); } catch (e) { continue; }
              if (parsed && typeof parsed === 'object' && !('fence' in parsed)
                  && parsed.type === type) return f.data;
            }
            return null;
        }""", type)
        return None if data is None else json.loads(data)

    def until(self, expression: str, arg: Any = None, *, timeout: int = QUICK) -> Any:
        """Poll ``expression`` (a JS function of ``arg``) until it returns a truthy value."""
        deadline = time.monotonic() + timeout / 1000
        while True:
            value = self.page.evaluate(expression, arg)
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout} ms waiting for {expression}")
            self.page.wait_for_timeout(25)

    def _wait(self, direction: str, type: str | None, since: int, where: dict | None,
              timeout: int) -> dict:
        self.until("([dir, type, since, where]) => {" + _MATCH_JS +
                   " return window.__gowuiTest.frames.some("
                   "(f) => matches(f, dir, type, since, where)); }",
                   [direction, type, since, where or {}], timeout=timeout)
        frames = self.sent(since) if direction == "sent" else self.received(since)
        for frame in frames:
            if (type is None or frame.get("type") == type) and all(
                    frame.get(k) == v for k, v in (where or {}).items()):
                return frame
        raise AssertionError(f"no {direction} {type} frame matching {where}")

    def wait_sent(self, type: str, since: int, where: dict | None = None,
                  timeout: int = QUICK) -> dict:
        """The first frame of ``type`` the page sent after ``since`` (matching ``where``)."""
        return self._wait("sent", type, since, where, timeout)

    def wait_received(self, type: str, since: int, where: dict | None = None,
                      timeout: int = QUICK) -> dict:
        return self._wait("received", type, since, where, timeout)

    def fence(self) -> int:
        """Round-trip a marker on the page's socket; returns the mark after the answer."""
        seq = self.page.evaluate("""() => {
            const record = window.__gowuiTest;
            const socket = record.sockets[record.sockets.length - 1];
            record.fences += 1;
            record.byTest = true;
            try {
              socket.send(JSON.stringify({ type: 'state', fence: record.fences }));
            } finally { record.byTest = false; }
            return record.seq;
        }""")
        self._wait("received", "state", seq, None, QUICK)
        return self.mark()

    def send_raw(self, frame: dict) -> None:
        """Send ``frame`` on the page's own socket (test setup, bypassing the controls). It is
        recorded as the test's, so ``sent`` and the protocol tripwire leave it out."""
        self.page.evaluate("""(frame) => {
            const record = window.__gowuiTest;
            record.byTest = true;
            try {
              record.sockets[record.sockets.length - 1].send(JSON.stringify(frame));
            } finally { record.byTest = false; }
        }""", frame)

    def act(self, frame: dict, *, timeout: int = QUICK) -> dict:
        """Send ``frame`` and return the next ``state`` broadcast."""
        since = self.mark()
        self.send_raw(frame)
        return self.wait_received("state", since, timeout=timeout)

    def state(self) -> dict:
        """The last ``state`` frame the page received."""
        state = self.last_received("state")
        assert state is not None, "the page has received no state"
        return state

    # -- the page ---------------------------------------------------------------------------------
    def t(self, key: str, variables: dict | None = None) -> str:
        """The page's own translation of ``key`` (texts §3.8 does not pin)."""
        return self.page.evaluate("([k, v]) => window.i18n.t(k, v || undefined)", [key, variables])

    def dataset(self, key: str, canvas: str = "#board") -> str | None:
        return self.page.locator(canvas).get_attribute(f"data-{kebab(key)}")

    def expect_dataset(self, key: str, value, *, timeout: int = QUICK, canvas: str = "#board"):
        expect(self.page.locator(canvas)).to_have_attribute(f"data-{kebab(key)}", value,
                                                            timeout=timeout)

    def draws(self, canvas: str = "#board") -> int:
        value = self.dataset("draws", canvas)
        assert value is not None and value.isdigit(), f"{canvas} has no draws record: {value!r}"
        return int(value)

    # -- routing (frames the server would not send on cue) ---------------------------------------
    def proxy_ws(self) -> None:
        """Route the page's WebSocket through the test: frames pass through to the real server
        both ways, and ``inject`` adds frames as if the server had sent them. Call before
        ``goto``."""
        def handler(route):
            server = route.connect_to_server()
            route.on_message(lambda message: server.send(message))
            server.on_message(lambda message: route.send(message))
            self.routes.append(route)
            self.servers.append(server)

        self.page.route_web_socket(re.compile(r".*/ws$"), handler)

    def inject(self, frame: dict) -> None:
        assert self.routes, "inject needs proxy_ws() before goto"
        self.routes[-1].send(json.dumps(frame))

    def center(self, canvas: str = "#board") -> tuple[float, float]:
        """The centre of the canvas on the page: the tengen of an odd board (§3.8 draws the grid
        symmetrically, with coordinates on every side)."""
        box = self.page.locator(canvas).bounding_box()
        assert box is not None, f"{canvas} is not rendered"
        return box["x"] + box["width"] / 2, box["y"] + box["height"] / 2


# -- analysis payloads (§2.2) for injected frames --------------------------------------------------------
def move_info(move: str, *, winrate: float | None = 0.5, score: float | None = 0.5,
              visits: int = 100, prior: float | None = 0.1, order: int = 0,
              pv: list[str] | None = None) -> dict:
    return {"move": move, "winrate": winrate, "scoreLead": score, "visits": visits,
            "prior": prior, "order": order, "pv": pv if pv is not None else [move]}


def analysis_payload(size: int, infos: list[dict], *, winrate: float | None = 0.5,
                     score: float | None = 0.0, visits: int = 500,
                     policy: list[float] | None = None, ownership: list[float] | None = None,
                     current: str = "", source: str = "gtp", compare: dict | None = None) -> dict:
    """An ``analysis`` payload in the §2.2 shape (``policy`` size² + 1 long, or empty)."""
    full = []
    for info in infos:
        full.append({"scoreMean": info.get("scoreLead"), "scoreStdev": None, "lcb": None,
                     "utility": None, "utilityLcb": None, **info})
    if policy is not None:
        assert len(policy) == size * size + 1, "a policy is size² + 1 long (§2.2)"
    if ownership:
        assert len(ownership) == size * size, "an ownership is size² long (§2.2)"
    return {
        "moveInfos": full,
        "rootInfo": {"visits": visits, "winrate": winrate, "scoreLead": score, "scoreMean": score},
        "ownership": ownership if ownership is not None else [],
        "policy": policy if policy is not None else [],
        "turn": 0,
        "complete": False,
        "source": source,
        "currentPlayer": current,
        "compare": compare,
    }


def analysis_frame(state: dict, payload: dict, *, cursor: int | None = None) -> dict:
    """An ``analysis`` frame (§4.2) for the position of ``state`` (or another ``cursor``)."""
    game = state["game"]
    return {"type": "analysis", "cursor": game["cursor"] if cursor is None else cursor,
            "toPlay": game["toPlay"], "analysis": payload}


def vertex(x: int, y: int, size: int) -> str:
    """GTP vertex of column ``x`` and row ``y`` counted from the top (y = 0 is row ``size``)."""
    return "ABCDEFGHJKLMNOPQRSTUVWXYZ"[x] + str(size - y)


def index_of(v: str, size: int) -> int:
    x = "ABCDEFGHJKLMNOPQRSTUVWXYZ".index(v[0])
    y = size - int(v[1:])
    return y * size + x


def post_sgf(url: str, text: str) -> dict:
    """Load ``text`` into the active board through ``POST /api/sgf`` (§5), as the page does."""
    import httpx

    response = httpx.post(url + "/api/sgf", content=text.encode("utf-8"), timeout=10,
                          trust_env=False)
    assert response.status_code == 200, (response.status_code, response.text)
    return response.json()
