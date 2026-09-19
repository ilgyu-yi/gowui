# gowui

A browser-based Go GUI for KataGo and handol-mux, with a local mode and a multi-user server mode.

## Status

Bootstrapped via `GHJig-Claude` stage-0 (`/bootstrap-repo`); the application is being built up issue by issue.

## Run (local mode)

```bash
gowui                      # same as `gowui local`; serves on 127.0.0.1:8080
gowui --fresh              # keep boards in memory only; no state file is read or written
gowui --state ./my.json    # use this state file instead of the per-user default
```

Once it is listening, gowui prints `gowui: http://<host>:<port>`. Open that URL in a browser.
Boards are saved to a per-user state file and come back after a restart. The flags, the default
state-file paths and the security rules are in `SPEC.md` §9, §8.3 and §7.4.

## Run (server mode)

```bash
export GOWUI_DB=./data/gowui.db
export GOWUI_ENGINES='[{"id": "katago", "label": "KataGo", "protocol": "analysis", "host": "10.0.0.5", "port": 6364}]'
gowui user add alice       # prompts twice for the password (or use --password-stdin)
gowui serve                # serves on 0.0.0.0:8080; users sign in at /login
```

Each account gets its own boards, saved in SQLite, and picks engines only from the catalog; the
engine addresses never reach the browser. For SSO behind a reverse proxy, set `GOWUI_AUTH=header`
(or `local,header`) and `GOWUI_TRUSTED_PROXIES`, and have the proxy strip any client-supplied
user header. Every variable, the sign-in rules and the proxy rules are in `SPEC.md` §10, §7 and §9;
`gowui serve` refuses to start on a malformed setting.

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests need no KataGo, GPU or model file.

### Frontend checks

The page (`gowui/static/`, `SPEC.md` §3.8) has two kinds of tests. The static ones — the syntax
check of every script and the tuple-validator agreement test — run in the default `pytest` and
need `node` on the `PATH`; without it they are skipped locally (in CI they fail instead). The
browser tests drive Chromium through Playwright, carry the `browser` marker, and are left out of
the default run:

```bash
pip install -e '.[dev,browser]'
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright"   # repo-local, gitignored
python -m playwright install --with-deps chromium    # --with-deps installs system libraries (Linux)
pytest -m browser
```

The browser smoke test saves a screenshot of the page under the gitignored `test-artifacts/`. The
one shown in a pull request is committed under `docs/screenshots/` and linked by its commit's URL.

### Engine configuration

gowui expects KataGo to report winrates from the side to move: set `reportAnalysisWinratesAs = SIDETOMOVE` (or leave it unset) in the engine's config. The stock `analysis_example.cfg` sets `BLACK` and must be changed (SPEC §2.3).

### Fake engine

`tools/fake_engine.py` is an in-repo stand-in engine that speaks the engine protocols over real TCP
(behaviour: `SPEC.md` §2.6). To run one by hand and point gowui at it:

```bash
python tools/fake_engine.py --protocol gtp --port 6363
```

`--port 0` picks a free port and prints it (`listening on 127.0.0.1:<port>`).

## Documentation

- [`MISSION.md`](MISSION.md) — canonical direction for this project.
- [`SPEC.md`](SPEC.md) — behavioural contract of both launch modes (single source of truth for behaviour).
