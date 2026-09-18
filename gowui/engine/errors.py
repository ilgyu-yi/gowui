"""The one error kind every engine client raises (SPEC §2.1)."""

from __future__ import annotations


class EngineError(RuntimeError):
    """An engine refused a command, timed out, or the connection failed.

    The message never names the engine's host or port; the address travels alongside in
    :attr:`address` so only an engine-address policy that exposes addresses shows it (§6.3, §7.7).
    """

    def __init__(self, message: str = "", *, address: tuple[str, int] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.address = address

    def copy(self) -> "EngineError":
        """A fresh error of the same kind, message and address (to raise it again elsewhere)."""
        return type(self)(self.message, address=self.address)


class ConnectionClosed(EngineError):
    """The connection is closed, was lost, or could not be opened."""
