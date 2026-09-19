"""The command line (SPEC §7.4, §9): choosing local mode, the flags and their defaults, the policy
bundle the flags build, the server settings, the non-loopback warning, the URL line, and one real
``python -m gowui`` run that serves, saves on SIGINT and exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import stat
import sys
from pathlib import Path

import httpx
import pytest

from session_helpers import sgf_of

REPO = Path(__file__).resolve().parent.parent
MIB = 1024 * 1024


def parse(*argv: str):
    from gowui.cli import parse as cli_parse

    return cli_parse(list(argv))


# -- choosing local (§9) -----------------------------------------------------------------------------
def test_no_arguments_is_local_mode():
    assert parse().command == "local"


def test_no_arguments_equals_local_with_no_flags():
    assert vars(parse()) == vars(parse("local"))


def test_a_first_argument_starting_with_a_dash_is_local_mode():
    assert vars(parse("--port", "9000")) == vars(parse("local", "--port", "9000"))


@pytest.mark.parametrize("flag,value", [
    ("host", "127.0.0.1"), ("port", 8080), ("engine_protocol", "gtp"),
    ("engine_host", "127.0.0.1"), ("engine_port", 6363), ("connect", False), ("state", None),
    ("fresh", False), ("log_level", "info"),
])
def test_the_local_flags_have_the_baseline_defaults(flag, value):
    assert getattr(parse(), flag) == value


def test_the_flags_are_read():
    args = parse("local", "--host", "0.0.0.0", "--port", "9000", "--engine-protocol", "handol",
                 "--engine-host", "10.0.0.2", "--engine-port", "7000", "--connect",
                 "--state", "/tmp/x.json", "--log-level", "debug")
    assert (args.host, args.port, args.engine_protocol, args.engine_host, args.engine_port,
            args.connect, str(args.state), args.fresh, args.log_level) == (
        "0.0.0.0", 9000, "handol", "10.0.0.2", 7000, True, "/tmp/x.json", False, "debug")


@pytest.mark.parametrize("protocol", ["gtp", "analysis", "handol"])
def test_every_registered_protocol_is_an_engine_protocol_choice(protocol):
    assert parse("--engine-protocol", protocol).engine_protocol == protocol


def test_the_engine_protocol_choices_are_the_registered_protocols(capsys):
    from gowui.engine import PROTOCOLS

    with pytest.raises(SystemExit):
        parse("--engine-protocol", "nope")
    err = capsys.readouterr().err
    assert all(name in err for name in PROTOCOLS)


def test_an_unregistered_engine_protocol_is_a_usage_error():
    with pytest.raises(SystemExit) as info:
        parse("--engine-protocol", "nope")
    assert info.value.code == 2


def test_state_and_fresh_together_are_a_usage_error():
    with pytest.raises(SystemExit) as info:
        parse("--state", "x.json", "--fresh")
    assert info.value.code == 2


def test_main_refuses_state_and_fresh_together():
    from gowui.cli import main

    with pytest.raises(SystemExit) as info:
        main(["--state", "x.json", "--fresh"])
    assert info.value.code == 2


# -- the bundle the flags build (§6.1, §6.3, §6.4) -------------------------------------------------
def build(*argv: str):
    from gowui.cli import build_app

    return build_app(parse(*argv))


def test_fresh_uses_the_memory_storage():
    from gowui.local_mode import MemoryStorage

    assert isinstance(build("--fresh").state.policies.storage, MemoryStorage)


def test_state_uses_a_file_storage_at_that_path(tmp_path):
    from gowui.local_mode import JsonFileStorage

    storage = build("--state", str(tmp_path / "s.json")).state.policies.storage
    assert (isinstance(storage, JsonFileStorage), Path(storage.path)) == (True,
                                                                          tmp_path / "s.json")


def test_without_state_flags_the_file_storage_uses_the_default_path(tmp_path, monkeypatch):
    from gowui.local_mode import default_state_path

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    storage = build().state.policies.storage
    assert Path(storage.path) == default_state_path(sys.platform, os.environ, Path.home())


def test_the_engine_flags_are_the_defaults_of_the_connect_form():
    engines = build("--fresh", "--engine-protocol", "analysis", "--engine-host", "10.0.0.2",
                    "--engine-port", "7000").state.policies.engines
    assert engines.describe() == {"kind": "typed", "defaults": {
        "protocol": "analysis", "host": "10.0.0.2", "port": 7000}}


def test_the_host_flag_joins_the_allowed_hosts():
    policies = build("--fresh", "--host", "MyBox.LAN").state.policies
    assert policies.allowed_hosts == frozenset({"localhost", "127.0.0.1", "::1", "mybox.lan"})


def test_an_ipv6_host_flag_joins_the_allowed_hosts_unbracketed():
    policies = build("--fresh", "--host", "::").state.policies
    assert "::" in policies.allowed_hosts


# -- server settings (§9) --------------------------------------------------------------------------
def config(*argv: str):
    from gowui.cli import build_config

    return build_config(parse(*argv))


def test_the_server_binds_the_host_and_port_flags():
    built = config("--fresh", "--host", "127.0.0.2", "--port", "0")
    assert (built.host, built.port) == ("127.0.0.2", 0)


def test_the_server_does_not_rewrite_forwarded_headers():
    assert config("--fresh").proxy_headers is False


def test_the_server_limits_websocket_messages_to_one_mib():
    assert config("--fresh").ws_max_size == MIB


def test_the_server_runs_lifespan_handlers():
    assert config("--fresh").lifespan == "on"


def test_the_server_sends_no_server_header():
    assert config("--fresh").server_header is False


def test_the_shared_builder_applies_the_same_settings():
    from gowui.app import create_app
    from gowui.cli import uvicorn_config

    from app_helpers import local_bundle

    built = uvicorn_config(create_app(local_bundle()), host="127.0.0.1", port=0)
    assert (built.proxy_headers, built.ws_max_size, built.lifespan) == (False, MIB, "on")


# -- the non-loopback warning (§7.4, §9) -------------------------------------------------------------
def warned(caplog, *argv: str) -> bool:
    with caplog.at_level(logging.WARNING):
        config("--fresh", *argv)
    return any(r.levelno >= logging.WARNING and "unauthenticated" in r.getMessage().lower()
               for r in caplog.records)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "mybox.lan", "::", "10.0.0.1"])
def test_binding_a_non_loopback_address_warns_that_the_app_is_unauthenticated(caplog, host):
    assert warned(caplog, "--host", host) is True


@pytest.mark.parametrize("host", ["127.0.0.1", "127.9.9.9", "localhost", "::1"])
def test_binding_a_loopback_address_does_not_warn(caplog, host):
    assert warned(caplog, "--host", host) is False


# -- the URL line (§9) -------------------------------------------------------------------------------
def test_the_url_line_names_the_host_and_port():
    from gowui.cli import url_line

    assert url_line("127.0.0.1", 8080) == "gowui: http://127.0.0.1:8080"


def test_the_url_line_brackets_an_ipv6_host():
    from gowui.cli import url_line

    assert url_line("::1", 8080) == "gowui: http://[::1]:8080"


# -- a real run: python -m gowui --port 0 --state FILE (§9) --------------------------------------------
@pytest.mark.skipif(sys.platform == "win32", reason="SIGINT to a child process")
async def test_python_m_gowui_serves_prints_its_url_and_saves_on_sigint(tmp_path):
    state = tmp_path / "state.json"
    stderr = (tmp_path / "stderr.txt").open("wb")
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "NO_PROXY": "*"}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gowui", "--port", "0", "--state", str(state),
        cwd=str(REPO), env=env, stdout=asyncio.subprocess.PIPE, stderr=stderr)
    try:
        line = (await asyncio.wait_for(proc.stdout.readline(), 20)).decode().strip()
        assert line.startswith("gowui: http://127.0.0.1:"), (
            f"no URL line: {line!r}; stderr: {(tmp_path / 'stderr.txt').read_text()[-2000:]}")
        url = line.removeprefix("gowui: ")
        async with httpx.AsyncClient(base_url=url, timeout=5, trust_env=False) as client:
            health = await client.get("/healthz")
            upload = await client.post("/api/sgf", content=sgf_of(9, "D4"))
        proc.send_signal(signal.SIGINT)
        await asyncio.wait_for(proc.wait(), 20)
        result = (health.json(), upload.status_code, state.exists(),
                  state.exists() and stat.S_IMODE(state.stat().st_mode))
        assert result == ({"ok": True}, 200, True, 0o600)
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 10)
        stderr.close()
