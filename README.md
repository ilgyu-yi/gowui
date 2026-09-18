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

## Documentation

- [`MISSION.md`](MISSION.md) — canonical direction for this project.
- [`SPEC.md`](SPEC.md) — behavioural contract of both launch modes (single source of truth for behaviour).
