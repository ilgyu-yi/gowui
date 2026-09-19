"""Engine clients (SPEC §2): KataGo GTP, the KataGo analysis engine and handol-mux, over TCP."""

from __future__ import annotations

from .analysis import AnalysisEngine
from .base import Engine
from .errors import ConnectionClosed, EngineError
from .gtp import GTPEngine
from .handol import HandolEngine
from .types import Analysis, MoveInfo, Position, RootInfo

#: Registered protocols.
PROTOCOLS: dict[str, type[Engine]] = {"gtp": GTPEngine, "analysis": AnalysisEngine,
                                      "handol": HandolEngine}


def create_engine(protocol: str, host: str, port: int, log=None) -> Engine:
    """A client for ``protocol``; an unregistered protocol is an :class:`EngineError`."""
    key = protocol.strip().lower() if isinstance(protocol, str) else ""
    if key not in PROTOCOLS:
        raise EngineError(f"unknown protocol {protocol!r}; expected one of "
                          f"{', '.join(PROTOCOLS)}")
    return PROTOCOLS[key](host, port, log)


__all__ = [
    "Analysis", "AnalysisEngine", "ConnectionClosed", "Engine", "EngineError", "GTPEngine",
    "HandolEngine",
    "MoveInfo", "PROTOCOLS", "Position", "RootInfo", "create_engine",
]
