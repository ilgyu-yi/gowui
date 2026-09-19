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

## Development

```bash
pip install -e '.[dev]'
pytest
```

The tests need no KataGo, GPU or model file.

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
