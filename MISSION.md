# Mission

## What this exists for
gowui is a browser-based Go GUI that does what gogui does for desktop users — connect to an engine, play, and show what the engine is thinking — rebuilt for KataGo (GTP and analysis-engine protocols) and the handol-mux human-policy surface. One repository and one application serve two launch modes: **local** (one person on their own machine, no login, any engine address) and **server** (a shared deployment where each signed-in account keeps its own boards and the server decides which engines exist). Without it, a Go player who wants to inspect KataGo or human-rank move distributions must either run a desktop GUI next to the engine or give up the multi-board, compare-two-tuples review workflow entirely.

## Success looks like
- `gowui` on a laptop opens a working board at `http://127.0.0.1:8080`, connects to a typed engine address, and brings back the same boards after a restart.
- `gowui serve` in the published container serves several accounts at once (password and/or SSO-header sign-in), each with isolated, persisted boards, and never reveals engine addresses to the browser.
- Every feature exists once: local and server mode share one app, one set of routes and one frontend; a mode only selects identity, engine and storage policy.
- The full test suite runs without KataGo, a GPU or a model file (a fake engine speaks all protocols over real TCP) and is green in CI on every PR.

## Explicitly NOT goals
- Running or bundling KataGo itself — engines are reached over TCP.
- A public, internet-scale service (user self-registration, billing, horizontal scaling).
- Letting server-mode users type arbitrary engine addresses (the server connects, so that would be an SSRF door).
- A build step for the frontend — it stays vanilla JS + Canvas.

## Stakeholders
- Primary user and decision maker: the repository owner (Go player reviewing games with KataGo / handol-mux).
- Secondary users: people signed in to a shared server deployment.

## Last reviewed: 2026-09-19
