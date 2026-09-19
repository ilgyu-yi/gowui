"""The one app factory (SPEC §5, §6.1): a policy bundle in, a FastAPI app out.

The factory takes no mode name. It builds the space registry, installs the routes, puts the guard
in front of everything — outside the server-error handler too, so a ``500`` carries the §7.5
headers — and runs the startup hooks in the lifespan startup, before the server listens. The
lifespan shutdown saves and closes every space (§8.2).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable

from fastapi import FastAPI
from starlette.types import ASGIApp

from . import routes
from .guard import Guard
from .policies import Policies
from .spaces import SpaceRegistry

__all__ = ["create_app"]

StartupHook = Callable[[FastAPI], Awaitable[None]]


class _GuardedApp(FastAPI):
    """FastAPI with the guard as the outermost layer, outside ``ServerErrorMiddleware``."""

    def build_middleware_stack(self) -> ASGIApp:
        return Guard(super().build_middleware_stack(), self.state.policies)


def create_app(policies: Policies, *, startup: Iterable[StartupHook] = ()) -> FastAPI:
    registry = SpaceRegistry(policies)
    hooks = list(startup)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await registry.start()
        try:
            for hook in hooks:
                await hook(app)
            yield
        finally:
            await registry.aclose()

    app = _GuardedApp(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.policies = policies
    app.state.registry = registry
    routes.install(app)
    return app
