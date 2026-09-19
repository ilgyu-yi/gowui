"""Every HTTP route and the WebSocket endpoint (SPEC §4.3, §5, §7.6); no other module declares
a route. The guard (§7.4) has already run: it put the request's identity in the scope.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI, Request, WebSocket
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse, Response
from starlette.staticfiles import StaticFiles

from .game import Game
from .guard import IDENTITY_KEY
from .rules import HANDICAP_KOMI, RULE_SETS
from .spaces import Space

__all__ = ["MAX_FRAME_BYTES", "MAX_UPLOAD_BYTES", "install", "router"]

#: A WebSocket frame and an SGF upload are at most 1 MiB (§7.6).
MAX_FRAME_BYTES = 1024 * 1024
MAX_UPLOAD_BYTES = 1024 * 1024
CLOSE_TOO_BIG = 1009
STATIC_DIR = Path(__file__).resolve().parent / "static"

router = APIRouter()


async def _space(conn: HTTPConnection) -> Space:
    return await conn.app.state.registry.get(conn.scope[IDENTITY_KEY])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


class _TooLarge(JSONResponse):
    """The ``413`` of an oversize upload (§7.6).

    The answer is sent at once, before the body is read. The body the client is still sending is
    then discarded, briefly and up to a bound, before the connection closes: closing a socket with
    unread data resets it, and the reset can destroy the answer before the client reads it.
    """

    DRAIN_BYTES = 4 * 1024 * 1024
    DRAIN_IDLE = 0.3

    def __init__(self) -> None:
        super().__init__({"error": "the SGF is larger than 1 MiB"}, status_code=413)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": self.status_code,
                    "headers": self.raw_headers})
        await send({"type": "http.response.body", "body": self.body, "more_body": True})
        drained = 0
        while drained <= self.DRAIN_BYTES:
            try:
                message = await asyncio.wait_for(receive(), self.DRAIN_IDLE)
            except asyncio.TimeoutError:
                break
            if message.get("type") != "http.request":
                break
            drained += len(message.get("body", b""))
            if not message.get("more_body"):
                break
        await send({"type": "http.response.body", "body": b""})


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"ok": True})


@router.get("/login")
async def login_page(request: Request) -> Any:
    return await request.app.state.policies.identity.login_page(request)


@router.post("/login")
async def login(request: Request) -> Any:
    return await request.app.state.policies.identity.login(request)


@router.post("/logout")
async def logout(request: Request) -> Any:
    return await request.app.state.policies.identity.logout(request)


@router.get("/api/health")
async def health(request: Request) -> JSONResponse:
    policies = request.app.state.policies
    identity = request.scope[IDENTITY_KEY]
    space = await _space(request)
    return JSONResponse({
        "ok": True,
        # the ``engine`` object of ``state``, redacted as that frame is (§7.7)
        "engine": space.session.attach_frames()[0]["engine"],
        "rules": sorted(RULE_SETS),
        "ruleDefaults": {name: rs.default_komi for name, rs in RULE_SETS.items()},
        "handicapKomi": HANDICAP_KOMI,
        "engineAddress": policies.engines.describe(),
        "console": bool(policies.engines.offers_console),
        "me": {"name": identity.name, "source": identity.source,
               "logout": identity.logout_kind, "logoutUrl": identity.logout_url},
    })


@router.get("/api/sgf")
async def download_sgf(request: Request) -> Response:
    space = await _space(request)
    body = space.session.game.to_sgf().encode("utf-8", errors="replace")
    return Response(body, headers={
        "Content-Type": "application/x-go-sgf; charset=utf-8",
        "Content-Disposition": 'attachment; filename="gowui.sgf"'})


@router.post("/api/sgf")
async def upload_sgf(request: Request) -> Response:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        return _TooLarge()
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_UPLOAD_BYTES:
            return _TooLarge()
    text = body.decode("utf-8", errors="replace")
    if not text.strip():
        return _error(400, "the SGF is empty")
    try:
        game = await asyncio.to_thread(Game.from_sgf, text)
    except Exception as exc:  # noqa: BLE001 - SGFError, ValueError, RecursionError ...
        return _error(400, f"Could not load SGF: {exc}"[:500])
    space = await _space(request)
    space.session.load_game(game)
    return JSONResponse({"ok": True, "moves": game.move_count})


@router.websocket("/ws")
async def websocket(ws: WebSocket) -> None:
    registry = ws.app.state.registry
    await ws.accept()

    async def send(text: str) -> None:
        await ws.send_text(text)

    async def close(code: int) -> None:
        await ws.close(code)

    tab = await registry.attach(ws.scope[IDENTITY_KEY], send, close)
    session = tab.space.session
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            text = message.get("text")
            if text is None:
                tab.push({"type": "error", "message": "a frame must be JSON text"})
                continue
            if len(text) > MAX_FRAME_BYTES or len(text.encode("utf-8")) > MAX_FRAME_BYTES:
                await close(CLOSE_TOO_BIG)
                break
            try:
                data = json.loads(text)
            except (ValueError, RecursionError):
                tab.push({"type": "error", "message": "a frame must be a JSON object"})
                continue
            if not isinstance(data, dict):
                tab.push({"type": "error", "message": "a frame must be a JSON object"})
                continue
            await session.handle(data)
    finally:
        await registry.detach(tab)


def install(app: FastAPI) -> None:
    """Add the routes, then the page and its static assets at ``/``."""
    app.include_router(router)
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
