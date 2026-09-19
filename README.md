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

`gowui serve [--host 0.0.0.0] [--port 8080] [--log-level info]` is configured only by the
environment variables below. Each account gets its own boards, saved in SQLite, and picks engines
only from the catalog; the engine addresses never reach the browser. `gowui serve` refuses to
start, with one line naming the variable, on a malformed setting. The sign-in and proxy rules are in
`SPEC.md` §7 and §9.

### Configuration

Unset or empty means the default. The exact rules for each value are in `SPEC.md` §10.

| Variable | Meaning | Default |
|---|---|---|
| `GOWUI_DB` | SQLite file | `./data/gowui.db` (container: `/data/gowui.db`) |
| `GOWUI_AUTH` | `local` (passwords), `header` (SSO proxy) or `local,header` | `local` |
| `GOWUI_AUTH_HEADER` | header the proxy sets to the user name | `X-authentik-username` |
| `GOWUI_TRUSTED_PROXIES` | comma-separated IPs/CIDRs of the proxies whose headers are believed; required for `header` | — |
| `GOWUI_LOGOUT_URL` | where log-out sends SSO users | — |
| `GOWUI_SESSION_DAYS` | password session lifetime, days | `14` |
| `GOWUI_COOKIE_SECURE` | `auto` (Secure over https), `1` or `0` | `auto` |
| `GOWUI_ENGINES` | JSON list of catalog entries `{id, label?, protocol, host, port, console?}` | `[]` |
| `GOWUI_IDLE_MINUTES` | release a space after this many minutes with no tab | `10` |

For SSO behind a reverse proxy, set `GOWUI_AUTH=header` (or `local,header`) and
`GOWUI_TRUSTED_PROXIES` to the proxy's own address, and have the proxy remove any client-sent
`GOWUI_AUTH_HEADER` before its forward auth sets it. A TLS proxy in front of password sign-in must
be listed in `GOWUI_TRUSTED_PROXIES` too, or set `GOWUI_COOKIE_SECURE=1`.

### Accounts

Password accounts live in `GOWUI_DB`; the commands read no other variable and work while the
server runs.

```bash
gowui user add alice                       # prompts twice; or --password-stdin reads one line
gowui user passwd alice [--password-stdin] # new password; signs the account out everywhere
gowui user remove alice                    # deletes the account, its sessions and its boards
gowui user list                            # one name per line
```

### Container

The `Dockerfile` builds the server-mode image: non-root (uid 10001), the database at
`/data/gowui.db` on the `/data` volume, `gowui serve` as the default command, and a health check on
`/healthz` (`SPEC.md` §10.1).

```bash
docker build -t gowui:local .
docker run -d --name gowui -p 127.0.0.1:8080:8080 -v gowui-data:/data \
  --read-only --tmpfs /tmp --cap-drop ALL --security-opt no-new-privileges \
  -e GOWUI_ENGINES='[{"id": "katago", "protocol": "analysis", "host": "10.0.0.5", "port": 6364}]' \
  gowui:local
docker exec -it gowui gowui user add alice                                  # prompts
printf '%s\n' "$PASSWORD" | docker exec -i gowui gowui user add bob --password-stdin
```

A bind mount on `/data` must be writable by uid 10001. `deploy/compose.password.yaml` runs it with
password sign-in on a loopback port; `deploy/compose.sso.yaml` puts it behind traefik with
Authentik forward auth, strips the client-sent user header first and trusts only traefik's fixed
address. Replace the example values before use.

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
