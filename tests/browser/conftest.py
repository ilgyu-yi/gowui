"""Fixtures for the browser tests (SPEC §3.8; README "Frontend checks").

Every test gets its own processes: a fake engine (``tools/fake_engine.py --port 0``, §2.6) when it
needs one and the app (``python -m gowui --port 0 --fresh``, §9), so no board, setting or engine
leaks from one test into the next. The page runs in headless Chromium through Playwright's sync
API, each test in a fresh browser context (fresh ``localStorage``) with the frame recorder of
browser_kit.py installed.

Every test also fails at teardown on a console error or a ``securitypolicyviolation`` (§7.5), and
on a frame type outside the §4.1 / §4.2 tables (the protocol tripwire of tests/
test_frontend_protocol.py, checked against what really crossed the socket).

- Browsers: ``PLAYWRIGHT_BROWSERS_PATH`` defaults to the repo's gitignored ``.playwright/`` when it
  is unset (CI sets it explicitly).
- Without Playwright or its Chromium the tests are skipped, unless ``GOWUI_BROWSER_REQUIRED=1``
  (the CI browser job), which turns that into a failure.
- Screenshots, and a trace of a failed test, go to the gitignored ``test-artifacts/``.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from browser_kit import ARTIFACTS, ENGINE, INIT_SCRIPT, QUICK, REPO, Gowui

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(REPO / ".playwright"))
REQUIRED = os.environ.get("GOWUI_BROWSER_REQUIRED") == "1"
STARTUP = 30.0


def unavailable(reason: str):
    if REQUIRED:
        pytest.fail(f"{reason} (GOWUI_BROWSER_REQUIRED=1)")
    pytest.skip(reason)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)


# -- processes ------------------------------------------------------------------------------------------
class Process:
    """A child process whose first stdout line matching ``ready`` is awaited."""

    def __init__(self, argv: list[str], ready: str, log: Path):
        self.log_path = log
        self._log = log.open("wb")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_PROXY": "*"}
        self.proc = subprocess.Popen(argv, cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                                     stderr=self._log, text=True)
        self._lines: queue.Queue[str] = queue.Queue()
        threading.Thread(target=self._pump, daemon=True).start()
        self.match = self._await(re.compile(ready))

    def _pump(self) -> None:
        for line in self.proc.stdout:
            self._lines.put(line.rstrip("\n"))

    def _await(self, pattern: re.Pattern) -> re.Match:
        seen = []
        while True:
            try:
                line = self._lines.get(timeout=STARTUP)
            except queue.Empty:
                self.stop()
                raise RuntimeError(f"{self.proc.args!r} printed no {pattern.pattern!r} line; "
                                   f"stdout {seen}; stderr {self.stderr()[-2000:]}") from None
            seen.append(line)
            found = pattern.search(line)
            if found:
                return found

    def stderr(self) -> str:
        with contextlib.suppress(Exception):
            self._log.flush()
            return self.log_path.read_text(errors="replace")
        return ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(10)
        self._log.close()


@dataclass
class Engine:
    protocol: str
    port: int
    process: Process


@dataclass
class App:
    url: str
    port: int
    process: Process


@pytest.fixture
def processes():
    started: list[Process] = []
    yield started
    for process in reversed(started):
        process.stop()


@pytest.fixture
def start_engine(processes, tmp_path):
    """``start_engine(protocol, *extra)`` runs a fake engine (§2.6) for this test only;
    ``extra`` holds more command-line options, such as ``--delay genmove=0.05``."""
    def start(protocol: str = "gtp", *extra: str) -> Engine:
        process = Process([sys.executable, str(REPO / "tools" / "fake_engine.py"),
                           "--protocol", protocol, "--port", "0", *extra],
                          r"^listening on 127\.0\.0\.1:(\d+)$",
                          tmp_path / f"engine-{protocol}-{len(processes)}.log")
        processes.append(process)
        return Engine(protocol, int(process.match.group(1)), process)

    return start


@pytest.fixture
def start_app(processes, tmp_path):
    """``start_app(engine=None, connect=False, *extra, fresh=True)`` runs ``python -m gowui
    --port 0 --fresh`` with the engine as the connect form's default, connected at startup when
    ``connect``; ``fresh=False`` leaves out ``--fresh`` (pass ``--state FILE`` in ``extra``)."""
    def start(engine: Engine | None = None, connect: bool = False, *extra: str,
              fresh: bool = True) -> App:
        argv = [sys.executable, "-m", "gowui", "--port", "0", "--log-level", "warning"]
        if fresh:
            argv.append("--fresh")
        if engine is not None:
            argv += ["--engine-protocol", engine.protocol, "--engine-port", str(engine.port)]
            if connect:
                argv.append("--connect")
        argv += list(extra)
        process = Process(argv, r"^gowui: (http://127\.0\.0\.1:(\d+))$",
                          tmp_path / f"app-{len(processes)}.log")
        processes.append(process)
        return App(process.match.group(1), int(process.match.group(2)), process)

    return start


@pytest.fixture
def start_server_app(processes, tmp_path):
    """``start_server_app(engines=[], users={name: password}, **env)`` adds the accounts with
    ``gowui user add --password-stdin`` and runs ``python -m gowui serve --host 127.0.0.1 --port 0``
    over a temporary database (§9, §10); ``env`` holds more ``GOWUI_*`` variables."""
    import json

    def start(engines: list | None = None, users: dict | None = None, **env: str) -> App:
        variables = {**os.environ, "GOWUI_DB": str(tmp_path / "server" / "gowui.db"),
                     "GOWUI_ENGINES": json.dumps(engines or [])}
        variables.update({f"GOWUI_{k.upper()}": v for k, v in env.items()})
        for name, password in (users or {}).items():
            subprocess.run([sys.executable, "-m", "gowui", "user", "add", name,
                            "--password-stdin"], input=password + "\n", text=True, check=True,
                           cwd=str(REPO), env=variables, capture_output=True, timeout=STARTUP)
        saved = dict(os.environ)
        os.environ.update(variables)
        try:
            process = Process([sys.executable, "-m", "gowui", "serve", "--host", "127.0.0.1",
                               "--port", "0", "--log-level", "warning"],
                              r"^gowui: (http://127\.0\.0\.1:(\d+))$",
                              tmp_path / f"server-{len(processes)}.log")
        finally:
            os.environ.clear()
            os.environ.update(saved)
        processes.append(process)
        return App(process.match.group(1), int(process.match.group(2)), process)

    return start


# -- the browser -------------------------------------------------------------------------------------
@pytest.fixture(scope="session")
def playwright_instance():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        unavailable("Playwright is not installed (pip install -e '.[browser]')")
    manager = sync_playwright()
    playwright = manager.start()
    yield playwright
    playwright.stop()


@pytest.fixture(scope="session")
def browser(playwright_instance):
    try:
        launched = playwright_instance.chromium.launch()
    except Exception as exc:  # noqa: BLE001 - a missing Chromium download
        unavailable(f"Chromium is not available ({str(exc).splitlines()[0]}); run "
                    "`python -m playwright install chromium` with PLAYWRIGHT_BROWSERS_PATH set")
    yield launched
    launched.close()


@pytest.fixture
def open_page(browser, request):
    """``open_page(app, locale="en-US", storage=None, init=None)`` returns a ``Gowui`` driver
    on a fresh context (not yet navigated: call ``.open()``). ``storage`` pre-fills
    ``localStorage``; ``init`` is an extra init script run before the page's own."""
    from playwright.sync_api import expect

    expect.set_options(timeout=QUICK)
    contexts = []
    pages: list[Gowui] = []

    def make(app: App, *, locale: str = "en-US", storage: dict | None = None,
             init: str | None = None, context=None) -> Gowui:
        if context is None:
            context = browser.new_context(locale=locale, accept_downloads=True,
                                          viewport={"width": 1400, "height": 1000})
            context.set_default_timeout(ENGINE)
            context.tracing.start(screenshots=True, snapshots=True)
            contexts.append(context)
            context.add_init_script(INIT_SCRIPT)
            if storage:
                context.add_init_script(
                    "(() => { const s = %s; if (!sessionStorage.getItem('__seeded')) {"
                    " sessionStorage.setItem('__seeded', '1');"
                    " Object.entries(s).forEach(([k, v]) => localStorage.setItem(k, v)); } })();"
                    % _json(storage))
            if init:
                context.add_init_script(init)
        page = context.new_page()
        driver = Gowui(page, app.url)
        pages.append(driver)
        return driver

    yield make

    failed = any(getattr(request.node, f"rep_{when}", None) is not None
                 and getattr(request.node, f"rep_{when}").failed for when in ("setup", "call"))
    name = re.sub(r"[^\w.-]+", "_", request.node.nodeid.split("::")[-1])
    problems: list[str] = []
    for driver in pages:
        problems += driver.problems()
        problems += _tripwire(driver)
    for index, context in enumerate(contexts):
        if failed:
            ARTIFACTS.mkdir(exist_ok=True)
            for n, page in enumerate(context.pages):
                with contextlib.suppress(Exception):
                    page.screenshot(path=str(ARTIFACTS / f"{name}-{index}-{n}-failed.png"))
            with contextlib.suppress(Exception):
                context.tracing.stop(path=str(ARTIFACTS / f"{name}-{index}-trace.zip"))
        else:
            with contextlib.suppress(Exception):
                context.tracing.stop()
        with contextlib.suppress(Exception):
            context.close()
    if problems and not failed:
        pytest.fail("console errors or CSP violations:\n" + "\n".join(problems), pytrace=False)


def _json(value) -> str:
    import json

    return json.dumps(value)


def _tripwire(driver: Gowui) -> list[str]:
    """Frame types seen on the wire outside the §4.1 / §4.2 tables."""
    import json

    from frontend_helpers import spec_table_types

    inbound = spec_table_types("### 4.1")
    outbound = spec_table_types("### 4.2")
    out = []
    try:
        frames = driver.frames(0)
    except Exception:  # noqa: BLE001 - a page that never loaded recorded nothing
        return out
    for frame in frames:
        if frame["dir"] == "close" or frame.get("byTest"):
            continue
        with contextlib.suppress(ValueError):
            kind = json.loads(frame["data"]).get("type")
            allowed = inbound if frame["dir"] == "sent" else outbound
            if kind not in allowed:
                out.append(f"frame type {kind!r} {frame['dir']} is not in SPEC §4")
    return out
