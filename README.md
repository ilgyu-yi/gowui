# gowui

A browser-based Go GUI for KataGo and handol-mux, with a local mode and a multi-user server mode.

## Status

Bootstrapped via `GHJig-Claude` stage-0 (`/bootstrap-repo`); the application is being built up issue by issue.

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
