"""One space's working state (SPEC §3): boards, settings, the engine connection and the traffic
log, driven by browser messages (§4.1) and reported through broadcast frames (§4.2).

The session knows nothing of launch modes (§6.1). It is given the engine-address policy as two
inputs -- a resolver that turns an engine request into an :class:`EngineTarget` (or refuses it with
an :class:`EngineRequestError`) and a flag saying whether engine addresses may be shown -- and a
``broadcast`` callable that fans a frame out to every attached tab (the hub is the transport's).

Engine work never runs on a caller's task (§3.2): :meth:`GameSession.handle` applies state changes
and broadcasts them at once, and hands engine commands to tasks the session owns, serialised by one
lock. Work that finishes after the position, the move epoch or the engine connection changed is
discarded. Every frame leaves through one choke point, :meth:`GameSession._emit`, which withholds
and scrubs engine addresses when the policy hides them (§7.7).
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import itertools
import json
import math
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import coords
from .board import BLACK, WHITE, IllegalMove
from .engine import Analysis, ConnectionClosed, Engine, EngineError, Position, create_engine
from .engine.human import MOVE_STYLES, check_policies, tuple_problem
from .game import MAX_MOVES, Game

__all__ = ["EngineRequestError", "EngineTarget", "GameSession", "clean_preferences"]

#: Lines of traffic log a space keeps, and how many a newly attached tab receives (§3.6).
MAX_LOG_LINES = 400
ATTACH_LOG_LINES = 100
#: Boards per space, board-name length and SGF size (§7.6).
MAX_BOARDS = 64
MAX_NAME = 40
MAX_SGF_BYTES = 1024 * 1024
#: Visits and the report interval (§7.6).
MAX_VISITS = 1_000_000
MIN_INTERVAL, MAX_INTERVAL = 0.1, 10.0
#: A console command (§3.5, §7.6).
MAX_RAW = 1000
#: Connect attempts that run at once per space: the live one and one superseded (§3.2, §4.1).
MAX_CONNECT_ATTEMPTS = 2
#: A result text the engine gives through ``final_score`` is kept to this many characters.
MAX_RESULT = 100
#: The SGF result grammar a ``final_score`` reply must match to be recorded (§3.5).
RESULT_PATTERN = re.compile(r"0|Draw|Void|\?|[BW]\+(?:\d+(?:\.\d+)?|R|Resign|T|Time|F|Forfeit)?")
#: A ``play`` vertex and a rule-set name are refused above these lengths before parsing (§4.1).
MAX_VERTEX = 8
MAX_RULES_NAME = 40
#: How much of a refused value an error message quotes (§4.1).
ECHO = 40
#: A restored engine request is a flat object of at most this many scalar entries (§8.1).
MAX_REQUEST_ENTRIES = 16
MAX_REQUEST_TEXT = 256
#: A handol-mux profile name (§2.5, §7.6).
PROFILE_PATTERN = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
#: The preferences a storage policy may keep (§4.1, §6.4, §8.5): the caps of §7.6.
MAX_PREFERENCES_BYTES = 64 * 1024
MAX_PRESETS = 64
MAX_PRESET_NAME = 40
MAX_LANG_NAME = 16
#: A preset tuple is checked as if searching, as the page's Import checks one (§3.8, §4.1), so a
#: λ preset is not refused over the session's own Visits setting.
PRESET_VISITS = 2
#: What no name a browser sends may hold (§4.1): a control character (C0 or C1), a lone surrogate
#: — text no non-ASCII serialiser can write — or whitespace other than the plain space a preset
#: name may hold inside it. A newline in a preset name would let it fake a line of the page's
#: confirmation dialogs (§3.8). A language name is a token and holds no space at all; the server
#: holds no list of languages, and a name no table of the page knows is ignored there (§8.5).
BAD_NAME_CHAR = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]|[^\S ]")
#: Trimmed off a name beside the whitespace ``str.strip`` knows: the page trims with JavaScript's
#: ``trim()``, which takes a byte-order mark, so a name ending in one would otherwise be stored
#: with it on one side and without it on the other (§3.3, §4.1). Written as an escape: as a
#: raw mark it is invisible in every editor and diff, and a tool that strips one would empty it.
TRIM_ALSO = "\ufeff"
#: Every codepoint ``str.strip()`` takes off, and the mark above. ``str.strip(chars)`` walks in
#: from each end and stops, so a name costs one pass whatever is inside it; a rename trims
#: before it truncates, so the whole 1 MiB a frame may carry reaches this (§7.6).
#: Written out rather than derived, so importing costs nothing on a CLI call; a test pins it
#: against ``str.isspace`` so a later Unicode revision fails loudly instead of drifting.
TRIM_CHARS = ("\t\n\v\f\r\x1c\x1d\x1e\x1f \x85\xa0\u1680"
              "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"
              "\u2028\u2029\u202f\u205f\u3000") + TRIM_ALSO
#: Thumbnail heatmaps are rounded to keep ``state`` small.
THUMB_DECIMALS = 3
#: How long closing an engine may take before the session stops waiting for it.
CLOSE_TIMEOUT = 10.0
#: What replaces a hidden engine address in text sent to a browser (§7.7).
HIDDEN = "[engine]"
#: A character that extends a host name: next to a match, the text names something else (§7.7).
_HOST_EDGE = r"[0-9A-Za-z._\-]"

DEFAULT_PROFILE = "preaz_1d"
DEFAULTS_ENGINE = {"maxVisits": 500, "reportInterval": 0.4, "includeOwnership": False,
                   "evalVisits": 200}
DEFAULTS_PLAY = {"blackIsEngine": False, "whiteIsEngine": False, "blackStyle": "human",
                 "whiteStyle": "human", "analysisEnabled": False}



class EngineRequestError(Exception):
    """An engine-address policy refused an engine request; the message is browser-safe."""

    def __init__(self, message: str = "the engine request was refused") -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class EngineTarget:
    """Where an accepted engine request leads (§6.3)."""

    protocol: str
    host: str
    port: int
    #: The request as accepted, stored in the snapshot and shown in ``state.engine.request``.
    request_echo: Any = None
    #: Whether the policy offers the console for this engine (§3.5, §7.7).
    console: bool = False


Resolver = Callable[[Any], EngineTarget]
#: Sync and non-blocking; it may raise, and its failures are contained (§3.2).
Broadcast = Callable[[dict], None]


class _Refused(Exception):
    """A browser message that is refused with an error and changes nothing."""


@dataclass
class LogLine:
    direction: str
    text: str
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "text": self.text, "at": self.at}


#: Every position a board takes gets a fresh number, so a token never repeats (§3.2).
_versions = itertools.count(1)


@dataclass
class BoardSlot:
    """One board: its game, its handol-mux settings and its last analysis."""

    id: int
    name: str
    game: Game
    profile: str = DEFAULT_PROFILE
    policy: dict = field(default_factory=dict)
    compare: dict | None = None
    #: Names the board's position; changed by every play, undo, navigation, load, new game,
    #: resignation and console command.
    version: int = field(default_factory=lambda: next(_versions))
    last_analysis: Analysis | None = None
    #: The version :attr:`last_analysis` describes.
    analysis_version: int = -1

    def changed(self) -> None:
        self.version = next(_versions)

    def current_analysis(self) -> Analysis | None:
        if self.last_analysis is not None and self.analysis_version == self.version:
            return self.last_analysis
        return None


# -- message field checks ---------------------------------------------------------------------
#: Tells an absent key from one carrying ``null``, where the two mean different things (§4.1).
_MISSING = object()


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return isinstance(value, int)  # a huge int is a number; it is clamped


def _is_finite(value: Any) -> bool:
    """A number that is finite as a float (a boolean is not a number)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _number(message: dict, key: str) -> Any:
    value = message.get(key)
    if not _is_number(value):
        raise _Refused(f"{key} must be a number")
    return value


def _clamp_int(value: Any, low: int, high: int) -> int:
    if isinstance(value, float):
        value = round(value)
    return int(max(low, min(high, value)))


def _clamp_float(value: Any, low: float, high: float) -> float:
    if isinstance(value, int) and not low <= value <= high:
        return float(low if value < low else high)
    return float(max(low, min(high, float(value))))


def _string(message: dict, key: str) -> str:
    value = message.get(key)
    if not isinstance(value, str):
        raise _Refused(f"{key} must be a string")
    return value


def _boolean(message: dict, key: str) -> bool:
    value = message.get(key)
    if not isinstance(value, bool):
        raise _Refused(f"{key} must be true or false")
    return value


def _board_id(message: dict) -> int:
    value = message.get("id")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Refused("id must be a board id")
    return value


def _colour(message: dict, key: str = "color") -> int | None:
    """The colour named by ``key``, or ``None`` when it is absent or null."""
    value = message.get(key)
    if value is None:
        return None
    if isinstance(value, str) and value.strip().lower() in ("b", "black"):
        return BLACK
    if isinstance(value, str) and value.strip().lower() in ("w", "white"):
        return WHITE
    raise _Refused(f"{key} must be black or white")


def _colour_name(colour: int) -> str:
    return "black" if colour == BLACK else "white"


def _letter(colour: int) -> str:
    return "B" if colour == BLACK else "W"


def _profile_ok(value: Any) -> bool:
    return isinstance(value, str) and PROFILE_PATTERN.fullmatch(value) is not None


def _trim(value: str) -> str:
    """``value`` without the leading and trailing space the page's ``trim()`` also takes off."""
    return value.strip(TRIM_CHARS)


def _name_ok(value: Any, limit: int, *, spaces: bool = False) -> bool:
    """A name as §4.1 takes one: 1 to ``limit`` characters, none of them refused."""
    if not isinstance(value, str) or not 1 <= len(value) <= limit:
        return False
    if not spaces and " " in value:
        return False
    return BAD_NAME_CHAR.search(value) is None


def _lang_ok(value: Any) -> bool:
    """A UI language name as §4.1 takes it; the page ignores one no table of its own names."""
    return _name_ok(value, MAX_LANG_NAME)


def _preset_problem(entry: Any) -> str | None:
    """The first problem with one tuple preset (§4.1), or ``None``."""
    if not isinstance(entry, dict) or set(entry) != {"name", "tuple"}:
        return "a preset is an object with a name and a tuple only"
    if not isinstance(entry["name"], str):
        return "a preset needs a name"
    name = _trim(entry["name"])  # the stored form, and what every rule below reads
    if not name:
        return "a preset needs a name"
    if len(name) > MAX_PRESET_NAME:
        return f"a preset name is at most {MAX_PRESET_NAME} characters"
    if not _name_ok(name, MAX_PRESET_NAME, spaces=True):
        return ("a preset name holds no control character, no lone surrogate and no whitespace "
                "beyond a plain space")
    problem = tuple_problem(entry["tuple"], PRESET_VISITS)
    if problem is not None:
        return f"refused the preset {name[:ECHO]!r}: {problem}"
    return None


def _stored_preset(entry: dict) -> dict:
    """One preset as it is stored: the trimmed name and a copy of its tuple."""
    return {"name": _trim(entry["name"]), "tuple": copy.deepcopy(entry["tuple"])}


def clean_preferences(value: Any) -> dict:
    """``value`` as preferences (§4.2), with whatever the rules of §4.1 refuse dropped.

    What a storage policy reads back is taken this way, not refused as a whole (§6.4): a
    hand-edited row loses the entries the rules refuse, as a browser-stored preset does when the
    page reads its list (§8.5).

    A name held twice is one of those rules: the write path refuses such a frame whole (§4.1),
    while here the later entry is dropped and the **first** kept. Keeping both would load a row
    the next save sends back whole and is refused whole, wedging preset saving until the user
    deleted the name; refusing here would turn that row into a load failure instead.
    """
    source = value if isinstance(value, dict) else {}
    lang = source.get("lang")
    raw = source.get("presets")
    presets = raw[:MAX_PRESETS] if isinstance(raw, list) else []
    kept: list[dict] = []
    seen: set[str] = set()
    for entry in presets:
        if _preset_problem(entry) is not None:
            continue
        stored = _stored_preset(entry)
        if stored["name"] in seen:
            continue
        seen.add(stored["name"])
        kept.append(stored)
    return {"lang": lang if _lang_ok(lang) else None, "presets": kept}


def _flat_request(value: Any) -> dict | None:
    """A restored engine request if it is a flat, small object of scalars, else ``None`` (§8.1).
    Checked without recursion, so a hostile snapshot cannot raise anything but ``ValueError``."""
    if not isinstance(value, dict) or len(value) > MAX_REQUEST_ENTRIES:
        return None
    out: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > MAX_REQUEST_TEXT:
            return None
        if isinstance(item, str):
            if len(item) > MAX_REQUEST_TEXT:
                return None
        elif isinstance(item, float):
            if not math.isfinite(item):
                return None
        elif item is not None and not isinstance(item, (bool, int)):
            return None
        out[key] = item
    return out


def _is_streaming(command: str) -> bool:
    """Console commands that stream reports (``kata-analyze``, ``lz-analyze``,
    ``kata-genmove_analyze``, ``kata-search_analyze`` ...): the console is request and reply."""
    parts = command.split()
    if parts and parts[0].isdigit():
        parts = parts[1:]
    return bool(parts) and "analyze" in parts[0].lower()


def position_of(game: Game) -> Position:
    """The position at the game's cursor, as an engine receives it."""
    size = game.size
    return Position(
        size=size,
        komi=game.komi,
        rules=game.rules.katago,
        initial_stones=[[_letter(c), coords.to_gtp((x, y), size)]
                        for c, x, y in game.setup_stones],
        moves=[[_letter(m.color), coords.to_gtp(m.point, size)]
               for m in game.moves[: game.cursor]],
        first_player=_letter(game.first_player),
    )


def _abort_engine(engine: Engine) -> None:
    """Drop ``engine``'s connections at once; a broken client must not break the session."""
    try:
        engine.abort()
    except Exception:  # noqa: BLE001 - the last resort; nothing more to do
        pass


class GameSession:
    """The state a space's browser tabs talk to (SPEC §3)."""

    def __init__(self, resolve_engine: Resolver, *, expose_address: bool = True,
                 broadcast: Broadcast | None = None,
                 preferences: dict | None = None) -> None:
        self._resolve = resolve_engine
        self.expose_address = bool(expose_address)
        self._broadcast = broadcast
        #: The identity's preferences when the storage policy keeps them, else ``None`` — the
        #: page then keeps the language and the presets in the browser (§6.4, §8.5).
        self._preferences = None if preferences is None else clean_preferences(preferences)
        self.boards: list[BoardSlot] = [BoardSlot(1, "Board 1", Game(19))]
        self.active_board = 1
        self._board_ids = itertools.count(2)
        self.engine_settings = dict(DEFAULTS_ENGINE)
        self.play_settings = dict(DEFAULTS_PLAY)
        self.engine: Engine | None = None
        self._target: EngineTarget | None = None
        #: The engine request last accepted (§6.3, §8.1) and whether the space should be
        #: connected to it (what the snapshot's ``connected`` says).
        self._request: Any = None
        #: What ``state.engine.request`` shows when addresses are hidden: the policy's echo of a
        #: request it resolved, else ``None`` (§4.2).
        self._shown_request: Any = None
        self._want_connected = False
        self.status = ""
        self.thinking = False
        self.log: deque[LogLine] = deque(maxlen=MAX_LOG_LINES)
        #: Bumped on every log line; keys the cached ``log_history`` text (§4.3 Log folding).
        self._log_version = 0
        self._history_cache: tuple[tuple[int, int], str | None] | None = None
        #: Every engine address this space resolved: hidden from browsers unless exposed (§7.7).
        self._hidden: set[tuple[str, int]] = set()
        self._lock = asyncio.Lock()
        #: Changed by every board switch, duplicate and delete (§3.2).
        self._epoch = 0
        #: Changed by every change of the players; only automatic moves check it (§3.2).
        self._players_epoch = 0
        #: Changed by every connect, disconnect and restore reconnect; the latest wins (§6.3).
        self._lifecycle = 0
        #: Analysis refreshes are coalesced: one task, and a flag saying another pass is wanted.
        self._analysis_wanted = False
        self._analysis_task: asyncio.Task | None = None
        self._pending: set[str] = set()
        #: The lifecycle generation of the pending connect; one superseded is no longer pending.
        self._connecting: int | None = None
        #: The generations of every connect attempt still running, live or superseded (§3.2).
        self._attempts: set[int] = set()
        #: Engines released and still being closed in the background; they share the bound
        #: with the connect attempts (§3.2).
        self._closing: set[Engine] = set()
        self._tasks: set[asyncio.Task] = set()
        self._auto_task: asyncio.Task | None = None
        #: Set by an unexpected failure: automatic play asks for no further move until re-armed.
        self._auto_halted = False
        self._closed = False

    # -- the outbound choke point (§4.2, §7.7) --------------------------------------------------
    def _scrub(self, text: str) -> str:
        """``text`` with every hidden engine host (any case) and ``host:port`` replaced.

        Matched on host boundaries: a letter, digit, ``.``, ``-`` or ``_`` against a match means
        the text names something longer, not the engine (§7.7), so a catalog host such as
        ``katago`` costs the word ``KataGo`` but leaves ``katagonaut`` alone.
        """
        if self.expose_address or not self._hidden or not isinstance(text, str):
            return text
        for host, port in self._hidden:
            quoted = re.escape(host)
            # The order is load-bearing, and a test pins it: the bare-host arm must run last,
            # because every other arm contains the host. Taking that substring first would leave
            # the surrounding punctuation and port standing.
            for pattern in (rf"\[{quoted}\]:{port}(?!\d)",
                            rf"(?<!{_HOST_EDGE}){quoted}:{port}(?!\d)",
                            # A printed address tuple, with the flow info and scope id an IPv6
                            # address adds: matched whole, so no port survives the host (§7.7).
                            rf"\('{quoted}', {port}(?:, \d+)*\)",
                            rf"(?<!{_HOST_EDGE}){quoted}(?![0-9A-Za-z_\-]|\.[0-9A-Za-z])"):
                text = re.sub(pattern, HIDDEN, text, flags=re.IGNORECASE)
        return text

    def _withheld(self, text: str) -> bool:
        """Whether a log line carries a hidden engine address (then it is never sent)."""
        if self.expose_address or not self._hidden:
            return False
        lower = text.lower()
        return any(host.lower() in lower for host, _ in self._hidden)

    def _redact(self, frame: dict) -> dict | None:
        """The frame as it may leave the session, or ``None`` when it is withheld."""
        if self.expose_address or not self._hidden:
            return frame
        kind = frame.get("type")
        if kind == "log":
            return None if self._withheld(frame["line"]["text"]) else frame
        if kind == "log_history":
            return {**frame, "lines": [line for line in frame["lines"]
                                       if not self._withheld(line["text"])]}
        if kind == "error":
            return {**frame, "message": self._scrub(frame.get("message", ""))}
        if kind == "state":
            return {**frame, "status": self._scrub(frame["status"]),
                    "engine": self._scrub_tree(frame["engine"])}
        return frame

    def _scrub_tree(self, value: Any) -> Any:
        if isinstance(value, str):
            return self._scrub(value)
        if isinstance(value, dict):
            return {k: self._scrub_tree(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._scrub_tree(v) for v in value]
        return value

    def _emit(self, frame: dict) -> None:
        """Send one frame to every tab; a failing broadcast is contained."""
        if self._broadcast is None:
            return
        out = self._redact(frame)
        if out is None:
            return
        try:
            result = self._broadcast(out)
        except Exception:  # noqa: BLE001 - one broken tab must not break the space
            return
        if inspect.iscoroutine(result):
            result.close()  # broadcast is sync (§3.2): an awaitable it returns is never run

    def _emit_state(self) -> None:
        self._emit(self.state_message())

    def _error(self, message: str) -> None:
        self._emit({"type": "error", "message": message})

    def _engine_failure(self, prefix: str, exc: BaseException) -> str:
        """Browser text for an engine error: its message, plus the address only when shown."""
        text = exc.message if isinstance(exc, EngineError) else str(exc)
        text = f"{prefix}: {text}" if text else prefix
        if self.expose_address:
            address = getattr(exc, "address", None)
            if address is None and self._target is not None:
                address = (self._target.host, self._target.port)
            if address is not None:
                text += f" ({address[0]}:{address[1]})"
        return text

    # -- frames --------------------------------------------------------------------------------
    @property
    def _active(self) -> BoardSlot:
        return next(s for s in self.boards if s.id == self.active_board)

    @property
    def preferences(self) -> dict | None:
        """What a save would store for this identity, or ``None`` when none are kept (§6.4)."""
        return None if self._preferences is None else copy.deepcopy(self._preferences)

    @property
    def game(self) -> Game:
        return self._active.game

    def _slot(self, board_id: int) -> BoardSlot | None:
        return next((s for s in self.boards if s.id == board_id), None)

    def state_message(self) -> dict[str, Any]:
        slot = self._active
        engine = self.engine
        target = self._target
        settings = self.engine_settings
        return {
            "type": "state",
            "game": slot.game.to_dict(),
            "engine": {
                "connected": engine is not None,
                "protocol": engine.protocol if engine is not None else "",
                "name": engine.name if engine is not None else "",
                "version": engine.version if engine is not None else "",
                "request": copy.deepcopy(self._request if self.expose_address
                                         else self._shown_request),
                "supportsGenmove": bool(engine is not None and engine.supports_genmove),
                "supportsFinalScore": bool(engine is not None and engine.supports_final_score),
                "console": bool(engine is not None and engine.supports_raw
                                and target is not None and target.console),
            },
            "settings": {
                **self.play_settings,
                "maxVisits": settings["maxVisits"],
                "reportInterval": settings["reportInterval"],
                "includeOwnership": settings["includeOwnership"],
                "evalVisits": settings["evalVisits"],
                "humanProfile": slot.profile,
                "humanPolicy": copy.deepcopy(slot.policy),
                "humanCompare": copy.deepcopy(slot.compare),
            },
            # Shared, not copied: the frame is serialised straight after, and ``_preferences`` is
            # replaced by a change, never changed in place, so a frame already made keeps what it
            # carried (§4.2).
            "preferences": self._preferences,
            "status": self.status,
            "thinking": self.thinking,
            # Every state refreshes the active thumbnail; inactive positions cannot change
            # (§3.3), so their heavy drawing fields arrive once in the attach snapshot (§4.2).
            "boards": [self._thumbnail(s) if s.id == self.active_board
                       else self._board_summary(s) for s in self.boards],
            "activeBoard": self.active_board,
        }

    @staticmethod
    def _board_summary(slot: BoardSlot) -> dict[str, Any]:
        """The fields a tile can change while its position is inactive (§4.2)."""
        game = slot.game
        return {
            "id": slot.id,
            "name": slot.name,
            "size": game.size,
            "cursor": game.cursor,
            "moveCount": game.move_count,
            "profile": slot.profile,
            "policy": copy.deepcopy(slot.policy),
            "compare": slot.compare is not None,
        }

    def _thumbnail(self, slot: BoardSlot) -> dict[str, Any]:
        game = slot.game
        analysis = slot.current_analysis()
        heat: list[float] = []
        winrate = None
        if analysis is not None:
            heat = [round(v, THUMB_DECIMALS) for v in analysis.policy[: game.size * game.size]]
            winrate = analysis.root.winrate
        last = game.last_move
        return {
            **self._board_summary(slot),
            "stones": list(game.board.stones),
            "lastMove": coords.to_gtp(last.point, game.size) if last else None,
            "toPlay": _colour_name(game.to_play),
            "heat": heat,
            "winrate": winrate,
        }

    def _thumbnail_snapshot(self) -> dict[str, Any]:
        """Every board drawing, sent once to a newly attached tab (§4.2)."""
        return {"type": "thumbnails", "boards": [self._thumbnail(s) for s in self.boards]}

    def _analysis_frame(self, slot: BoardSlot, analysis: Analysis) -> dict[str, Any]:
        return {"type": "analysis", "cursor": slot.game.cursor,
                "toPlay": _colour_name(slot.game.to_play), "analysis": analysis.to_dict()}

    def attach_frames(self) -> list[dict]:
        """What a newly attached tab receives: ``state``, the last log lines, and the last
        analysis if it still describes the position (§4.2)."""
        frames = [self.state_message(), self._thumbnail_snapshot(), self._log_history()]
        slot = self._active
        analysis = slot.current_analysis()
        if analysis is not None and self.play_settings["analysisEnabled"]:
            frames.append(self._analysis_frame(slot, analysis))
        return [f for f in (self._redact(frame) for frame in frames) if f is not None]

    def _log_history(self) -> dict:
        lines = [line.to_dict() for line in self.log][-ATTACH_LOG_LINES:]
        return {"type": "log_history", "lines": lines}

    def log_history_text(self) -> str | None:
        """The attach ``log_history`` frame, redacted, as JSON text; synchronous and cached until
        the log or the hidden addresses change, so every tab shares one encoding (§4.3).

        The key counts the hidden addresses, which is enough only because ``_hidden`` never
        loses one: an address a space resolved stays hidden for the life of the space (§7.7). A
        removal would need a version counter here instead.
        """
        key = (self._log_version, len(self._hidden))
        if self._history_cache is None or self._history_cache[0] != key:
            frame = self._redact(self._log_history())
            self._history_cache = (key, None if frame is None else json.dumps(frame))
        return self._history_cache[1]

    # -- the traffic log (§3.6) ----------------------------------------------------------------
    def _record_log(self, direction: str, text: str) -> None:
        """Engine traffic callback: runs inside engine tasks, so it only records and sends."""
        if self._withheld(text):
            return
        line = LogLine(direction, text)
        self.log.append(line)
        self._log_version += 1
        self._emit({"type": "log", "line": line.to_dict()})

    # -- tasks the session owns (§3.2) -------------------------------------------------------------
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and not self._closed:
            self._unexpected(exc)

    def _unexpected(self, exc: BaseException) -> None:
        """An unexpected failure in a session task: clear thinking, report, stop auto-play.
        Auto-play is halted with a flag it checks between moves, never cancelled mid-command."""
        self.thinking = False
        self._auto_halted = True
        self._error(f"internal error ({type(exc).__name__})")
        self._emit_state()

    # -- messages (§4.1) -------------------------------------------------------------------------
    async def handle(self, message: Any) -> None:
        """Handle one browser message. Never raises; engine work finishes in the background."""
        try:
            if self._closed:
                return
            if not isinstance(message, dict):
                raise _Refused("a message must be a JSON object")
            kind = message.get("type")
            handler = self._HANDLERS.get(kind) if isinstance(kind, str) else None
            if handler is None:
                raise _Refused(f"unknown message type {str(kind)[:40]!r}")
            result = handler(self, message)
            if inspect.isawaitable(result):
                await result
        except _Refused as exc:
            self._error(str(exc))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - one failing command never breaks the space
            self._error(f"internal error ({type(exc).__name__})")

    # -- game commands ------------------------------------------------------------------------------
    def _position_changed(self, slot: BoardSlot, status: str | None = None, *,
                          rearm: bool = True) -> None:
        slot.changed()
        if status is not None:
            self.status = status
        self._emit_state()
        if rearm:
            self._maybe_engine_move()
        self._request_analysis()

    def _msg_play(self, message: dict) -> None:
        colour = _colour(message)
        if colour is None:
            raise _Refused("color must be black or white")
        vertex = _string(message, "vertex")
        if len(vertex) > MAX_VERTEX:
            raise _Refused(f"bad vertex {vertex[:ECHO]!r}: a vertex is at most {MAX_VERTEX} "
                           "characters")
        if vertex.strip().lower() == "resign":
            raise _Refused("resign is an action, not a vertex: send resign")
        self._play(colour, vertex)

    def _play(self, colour: int, vertex: str) -> None:
        slot = self._active
        game = slot.game
        try:
            point = coords.from_gtp(vertex, game.size)
        except ValueError as exc:
            raise _Refused(str(exc)) from None
        branched = game.cursor
        note = "" if game.cursor == game.move_count else (
            f"Branched at move {branched}; the later moves were discarded")
        try:
            game.play(colour, point)
        except IllegalMove as exc:
            raise _Refused(f"Illegal move {vertex}: {exc}") from None
        self._position_changed(slot, note)

    def _msg_pass(self, message: dict) -> None:
        colour = _colour(message)
        self._play(self.game.to_play if colour is None else colour, "pass")

    def _msg_resign(self, message: dict) -> None:
        colour = _colour(message)
        slot = self._active
        colour = slot.game.to_play if colour is None else colour
        slot.game.resign(colour)
        self._position_changed(slot, f"{_colour_name(colour).capitalize()} resigns")

    def _msg_undo(self, message: dict) -> None:
        slot = self._active
        slot.game.undo()
        # Undo never re-arms the engine (§3.2).
        self._position_changed(slot, "", rearm=False)

    def _msg_navigate(self, message: dict) -> None:
        index = message.get("index")
        if isinstance(index, bool) or not isinstance(index, int):
            raise _Refused("index must be a whole number")
        slot = self._active
        slot.game.navigate(max(-1, min(index, MAX_MOVES + 1)))
        self._position_changed(slot, "")

    def _msg_new_game(self, message: dict) -> None:
        size = message.get("size", 19)
        if isinstance(size, bool) or not isinstance(size, int):
            raise _Refused("size must be a whole number")
        komi = message.get("komi")
        if komi is not None and not _is_finite(komi):
            raise _Refused("komi must be a finite number")
        rules = message.get("rules", "japanese")
        if not isinstance(rules, str):
            raise _Refused("rules must be a rule-set name")
        if len(rules) > MAX_RULES_NAME:
            raise _Refused(f"unknown rule set {rules[:ECHO]!r}: a rule-set name is at most "
                           f"{MAX_RULES_NAME} characters")
        handicap = message.get("handicap", 0)
        if handicap is None:
            handicap = 0
        if isinstance(handicap, bool) or not isinstance(handicap, int):
            raise _Refused("handicap must be a whole number")
        try:
            game = Game(size, komi=komi, rules=rules, handicap=handicap)
        except (ValueError, OverflowError) as exc:
            raise _Refused(str(exc)[:500]) from None
        slot = self._active
        slot.game = game
        slot.last_analysis = None
        self._position_changed(slot, f"New {game.size}x{game.size} game, komi {game.komi:g}, "
                                     f"{game.rules.name} rules")

    async def _msg_load_sgf(self, message: dict) -> None:
        text = _string(message, "sgf")
        # A lone surrogate counts as its three UTF-8 bytes; a download writes it as ``?`` (§5).
        if len(text.encode("utf-8", errors="surrogatepass")) > MAX_SGF_BYTES:
            raise _Refused("Could not load SGF: the file is larger than 1 MiB")
        try:
            game = await asyncio.to_thread(Game.from_sgf, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - SGFError, ValueError, RecursionError ...
            raise _Refused(f"Could not load SGF: {exc}"[:500]) from None
        self.load_game(game)

    def load_game(self, game: Game) -> None:
        """Replace the active board's game with one already read (the HTTP upload route parses
        its body off the event loop and then calls this)."""
        if self._closed:
            return
        slot = self._active
        slot.game = game
        slot.last_analysis = None
        self._position_changed(slot, f"Loaded {game.move_count} moves")

    def _msg_state(self, message: dict) -> None:
        self._emit_state()

    # -- settings -------------------------------------------------------------------------------------
    def _msg_analysis(self, message: dict) -> None:
        enabled = _boolean(message, "enabled")
        self.play_settings["analysisEnabled"] = enabled
        if not enabled:
            self._active.last_analysis = None
        self._emit_state()
        self._request_analysis()

    def _msg_players(self, message: dict) -> None:
        changes: dict[str, Any] = {}
        for key in ("blackIsEngine", "whiteIsEngine"):
            if message.get(key) is not None:
                changes[key] = _boolean(message, key)
        for key in ("blackStyle", "whiteStyle"):
            if message.get(key) is not None:
                value = message.get(key)
                if value not in MOVE_STYLES:
                    raise _Refused(f"{key} must be human or katago")
                changes[key] = value
        if any(self.play_settings[k] != v for k, v in changes.items()):
            # A change of the players makes an automatic move in flight stale; an explicit
            # genmove is unaffected by the players setting (§3.2).
            self._players_epoch += 1
        self.play_settings.update(changes)
        self._configure_engine()
        self._emit_state()
        self._maybe_engine_move()
        self._request_analysis()

    def _msg_engine_params(self, message: dict) -> None:
        changes: dict[str, Any] = {}
        if message.get("maxVisits") is not None:
            changes["maxVisits"] = _clamp_int(_number(message, "maxVisits"), 1, MAX_VISITS)
        if message.get("reportInterval") is not None:
            changes["reportInterval"] = _clamp_float(_number(message, "reportInterval"),
                                                     MIN_INTERVAL, MAX_INTERVAL)
        if message.get("includeOwnership") is not None:
            changes["includeOwnership"] = _boolean(message, "includeOwnership")
        if "maxVisits" in changes:
            # The board's tuples are checked against the new value, so the refusal arrives once,
            # here, instead of on every later query (§2.5 "Settings validation", §3.4).
            slot = self._active
            tuples = [slot.policy] + ([slot.compare] if slot.compare is not None else [])
            try:
                check_policies(tuples, changes["maxVisits"])
            except EngineError as exc:
                raise _Refused(exc.message) from None
        self.engine_settings.update(changes)
        self._configure_engine()
        self._emit_state()
        self._request_analysis()

    def _msg_human_params(self, message: dict) -> None:
        slot = self._active
        profile, policy, compare = slot.profile, slot.policy, slot.compare
        eval_visits = self.engine_settings["evalVisits"]
        if message.get("profile") is not None:
            profile = message.get("profile")
            if not _profile_ok(profile):
                raise _Refused("the profile must be a name of 1-64 letters, digits, "
                               "'_', '.' or '-'")
        if "policy" in message and message.get("policy") is not None:
            policy = message.get("policy")
            problem = tuple_problem(policy, self.engine_settings["maxVisits"])
            if problem is not None:
                raise _Refused(f"refused the policy tuple: {problem}")
        if "compare" in message:
            compare = message.get("compare")
            if compare is not None:
                problem = tuple_problem(compare, self.engine_settings["maxVisits"])
                if problem is not None:
                    raise _Refused(f"refused the compare tuple: {problem}")
        if message.get("evalVisits") is not None:
            eval_visits = _clamp_int(_number(message, "evalVisits"), 0, MAX_VISITS)
        try:
            check_policies([policy] + ([compare] if compare is not None else []),
                           self.engine_settings["maxVisits"])
        except EngineError as exc:
            raise _Refused(exc.message) from None
        slot.profile = profile
        slot.policy = copy.deepcopy(policy)
        slot.compare = copy.deepcopy(compare)
        self.engine_settings["evalVisits"] = eval_visits
        self._configure_engine()
        self._emit_state()
        self._request_analysis()

    def _msg_preferences(self, message: dict) -> None:
        """Change the identity's language and tuple presets (§4.1); every field is optional.

        A field that is absent is unchanged. ``lang`` is present, not absent, when it is
        ``null``: that clears the stored language, the one way ``state.preferences.lang`` goes
        back to ``null`` (§4.2).

        Refused whole, changing nothing, when the storage policy keeps no preferences or when
        anything in the message is outside the bounds of §7.6 — unlike a stored value, which is
        read with what the same rules refuse dropped (§6.4).
        """
        if self._preferences is None:
            raise _Refused("preferences are kept in this browser, not for the account")
        if len(json.dumps(message)) > MAX_PREFERENCES_BYTES:
            raise _Refused(f"the preferences are larger than {MAX_PREFERENCES_BYTES} bytes")
        preferences = copy.deepcopy(self._preferences)
        if "lang" in message:
            if message["lang"] is not None and not _lang_ok(message["lang"]):
                raise _Refused(f"the language must be null or a name of 1 to {MAX_LANG_NAME} "
                               "characters without spaces, control characters or lone surrogates")
            preferences["lang"] = message["lang"]
        if "presets" in message:
            presets = message["presets"]
            if not isinstance(presets, list):
                raise _Refused("the presets must be a list")
            if len(presets) > MAX_PRESETS:
                raise _Refused(f"at most {MAX_PRESETS} presets are kept")
            for entry in presets:
                problem = _preset_problem(entry)
                if problem is not None:
                    raise _Refused(problem)
            stored = [_stored_preset(p) for p in presets]
            names = {p["name"] for p in stored}
            if len(names) != len(stored):
                # The page keys presets by name, so a duplicate would be deleted with the one it
                # shadows (§4.1); only a crafted frame or a hand-edited row can hold one.
                raise _Refused("two presets have the same name")
            preferences["presets"] = stored
        self._preferences = preferences
        self._emit_state()

    def _configure_engine(self, engine: Engine | None = None) -> None:
        """Hand a handol-mux engine the active board's settings (§3.4); only stores."""
        engine = engine if engine is not None else self.engine
        configure = getattr(engine, "configure", None)
        if configure is None:
            return
        slot = self._active
        configure(profile=slot.profile, policy=copy.deepcopy(slot.policy),
                  compare=copy.deepcopy(slot.compare),
                  eval_visits=self.engine_settings["evalVisits"],
                  max_visits=self.engine_settings["maxVisits"],
                  move_style={"B": self.play_settings["blackStyle"],
                              "W": self.play_settings["whiteStyle"]})

    # -- boards (§3.3) ------------------------------------------------------------------------------
    def _switch_to(self, slot: BoardSlot) -> None:
        # An engine move still searching belongs to the board left: it is discarded.
        self._epoch += 1
        self.active_board = slot.id
        self.status = ""
        self._configure_engine()
        self._emit_state()
        analysis = slot.current_analysis()
        if analysis is not None and self.play_settings["analysisEnabled"]:
            self._emit(self._analysis_frame(slot, analysis))
        self._maybe_engine_move()
        self._request_analysis()

    def _msg_board_select(self, message: dict) -> None:
        slot = self._slot(_board_id(message))
        if slot is None:
            raise _Refused(f"no board {str(message.get('id'))[:ECHO]}")
        self._switch_to(slot)

    def _msg_board_duplicate(self, message: dict) -> None:
        # ``board_duplicate`` names the board it copies, as ``board_delete`` does: it has no
        # "the active one" meaning (§4.1 "The board frames").
        source = self._slot(_board_id(message))
        if source is None:
            raise _Refused(f"no board {str(message.get('id'))[:ECHO]}")
        if len(self.boards) >= MAX_BOARDS:
            raise _Refused(f"cannot duplicate a board: a space holds at most {MAX_BOARDS} boards")
        board_id = self._fresh_id()
        clone = BoardSlot(board_id, f"Board {board_id}", copy.deepcopy(source.game),
                          source.profile, copy.deepcopy(source.policy),
                          copy.deepcopy(source.compare), source.version, source.last_analysis,
                          source.analysis_version)
        self.boards.insert(self.boards.index(source) + 1, clone)
        self._switch_to(clone)

    def _fresh_board(self, board_id: int) -> BoardSlot:
        """A fresh board with ``board_id`` (§3.3 "A fresh board"), the shape ``board_new`` and the
        reset of the last board both produce.

        An empty game at the **active** board's size and rules — a person reviewing 9×9 Chinese
        games wants another of those — with the **rule set's** default komi for them (what
        ``komi: null`` gets, §1.2), never the active board's own komi, which on a handicap board
        is 0.5 and would arrive here with no handicap stones. Handicap 0, no setup stones, the
        name ``Board <id>``, the default profile and empty tuples (the slot's own defaults).
        """
        game = self._active.game
        return BoardSlot(board_id, f"Board {board_id}",
                         Game(game.size, komi=None, rules=game.rules.name, handicap=0))

    def _msg_board_new(self, message: dict) -> None:
        if len(self.boards) >= MAX_BOARDS:
            raise _Refused(f"cannot add a new board: a space holds at most {MAX_BOARDS} boards")
        slot = self._fresh_board(self._fresh_id())
        self.boards.append(slot)
        self._switch_to(slot)

    def _msg_board_move(self, message: dict) -> None:
        """Put a board after the board ``after`` names, or at the head when it is null (§3.3
        "Move"). The order changes and nothing else: the active board stays active, no analysis
        is discarded and no engine is reconfigured (§4.1)."""
        slot = self._slot(_board_id(message))
        if slot is None:
            raise _Refused(f"no board {str(message.get('id'))[:ECHO]}")
        # ``after`` must be present: absent is refused rather than read as null, so a dropped
        # field cannot silently mean "move to the top" (§4.1).
        after = message.get("after") if "after" in message else _MISSING
        anchor: BoardSlot | None = None
        if after is not None:
            if after is _MISSING or isinstance(after, bool) or not isinstance(after, int):
                raise _Refused("after must be a board id or null")
            anchor = self._slot(after)
            if anchor is None:
                raise _Refused(f"no board {str(after)[:ECHO]}")
            if anchor is slot:
                raise _Refused("a board cannot be moved after itself")
        # An anchor already the board's predecessor lands it back where it was: the no-op a drag
        # that lands where it started produces, accepted and changing the order not at all.
        self.boards.remove(slot)
        self.boards.insert(0 if anchor is None else self.boards.index(anchor) + 1, slot)
        self._emit_state()

    def _fresh_id(self) -> int:
        taken = {s.id for s in self.boards}
        while True:
            board_id = next(self._board_ids)
            if board_id not in taken:
                return board_id

    def _msg_board_delete(self, message: dict) -> None:
        slot = self._slot(_board_id(message))
        if slot is None:
            raise _Refused(f"no board {str(message.get('id'))[:ECHO]}")
        if len(self.boards) == 1:
            # Deleting the last board resets it in place (§3.3 "Delete"): it keeps its id — so a
            # tab holding that id does not lose its tile — its place and its active status, and
            # becomes a fresh board. The engine is reconfigured from the cleared settings (§3.4),
            # which ``_switch_to`` does along with the state frame.
            self.boards[0] = self._fresh_board(slot.id)
            self._switch_to(self.boards[0])
            return
        index = self.boards.index(slot)
        self.boards.remove(slot)
        self._epoch += 1
        if slot.id == self.active_board:
            self._switch_to(self.boards[min(index, len(self.boards) - 1)])
        else:
            self._emit_state()

    def _msg_board_rename(self, message: dict) -> None:
        board_id = _board_id(message)
        # Trimmed before the cut, so the 1 MiB a frame may carry cannot fill the 40 with space
        # (§7.6), and again after it: the cut falls wherever the 40th character is and can leave
        # the space that was between two words at the end (§3.3).
        name = _trim(_trim(_string(message, "name"))[:MAX_NAME])
        slot = self._slot(board_id)
        if slot is None:
            raise _Refused(f"no board {str(board_id)[:ECHO]}")
        if name:
            slot.name = name
        self._emit_state()

    # -- engine play (§3.2) ------------------------------------------------------------------------
    def _engine_plays(self, colour: int) -> bool:
        return self.play_settings["blackIsEngine" if colour == BLACK else "whiteIsEngine"]

    def _engine_should_move(self) -> bool:
        """Is it an engine's turn at the end of a game it can still play?"""
        engine = self.engine
        if engine is None or not engine.supports_genmove or self._closed:
            return False
        game = self.game
        if game.cursor != game.move_count or game.is_game_over() or game.cursor >= MAX_MOVES:
            return False
        return self._engine_plays(game.to_play)

    def _auto_running(self) -> bool:
        return self._auto_task is not None and not self._auto_task.done()

    def _maybe_engine_move(self) -> None:
        """Re-arm automatic play: every caller is a user action or a connect, never the engine's
        own automatic move, so a halt is cleared even while the halted task is finishing."""
        if not self._engine_should_move():
            return
        self._auto_halted = False
        if not self._auto_running():
            self._auto_task = self._spawn(self._auto_play())

    async def _auto_play(self) -> None:
        """Play on while the side to move is an engine; a discarded move just looks again."""
        while True:
            if self._auto_halted or not self._engine_should_move():
                return
            if await self._engine_move(explicit=False) == "failed":
                return

    def _msg_genmove(self, message: dict) -> None:
        colour = _colour(message)
        engine = self.engine
        if engine is None:
            raise _Refused("no engine is connected")
        if not engine.supports_genmove:
            raise _Refused("the engine cannot generate moves")
        game = self.game
        if colour is not None and colour != game.to_play:
            raise _Refused(f"it is {_colour_name(game.to_play)}'s turn, so the engine cannot "
                           f"move for {_colour_name(colour)}")
        if game.cursor >= MAX_MOVES:
            raise _Refused(f"a game holds at most {MAX_MOVES:,} moves")
        self._on_demand("genmove", self._explicit_genmove(colour))

    def _on_demand(self, kind: str, coro) -> None:
        """Run one on-demand command in the background; one pending per kind (§4.1)."""
        if kind in self._pending:
            coro.close()
            raise _Refused(f"a {kind} command is already running")
        self._pending.add(kind)

        async def run() -> None:
            try:
                await coro
            finally:
                self._pending.discard(kind)

        self._spawn(run())

    async def _explicit_genmove(self, colour: int | None) -> None:
        if await self._engine_move(explicit=True, colour=colour) == "played":
            self._maybe_engine_move()

    async def _ask_move(self, engine: Engine, position: Position, letter: str,
                        sink: Callable[[Analysis], None]) -> str:
        """One engine move with an explicit max visits (§3.4), by capability."""
        visits = self.engine_settings["maxVisits"]
        set_max_visits = getattr(engine, "set_max_visits", None)
        if set_max_visits is not None:
            await set_max_visits(visits)
        genmove_analyze = getattr(engine, "genmove_analyze", None)
        if genmove_analyze is not None and self.play_settings["analysisEnabled"]:
            return await genmove_analyze(
                position, letter, sink, interval=self.engine_settings["reportInterval"],
                include_ownership=self.engine_settings["includeOwnership"])
        if "max_visits" in inspect.signature(engine.genmove).parameters:
            return await engine.genmove(position, letter, max_visits=visits)
        return await engine.genmove(position, letter)

    async def _engine_move(self, *, explicit: bool, colour: int | None = None) -> str:
        """Ask for one move and play it: ``played``, ``stale`` (the position, the move epoch or
        the engine changed while it searched) or ``failed``."""
        async with self._lock:
            engine = self.engine
            if engine is None or self._closed:
                return "failed"
            slot = self._active
            game = slot.game
            if not explicit and (self._auto_halted or not self._engine_should_move()):
                return "stale"
            if colour is not None and colour != game.to_play:
                self._error(f"it is {_colour_name(game.to_play)}'s turn now; the engine move "
                            "was not asked for")
                return "failed"
            mover = game.to_play
            token = (slot.id, slot.version, self._epoch, self._players_epoch)
            sink = self._analysis_sink(slot, engine)
            self.thinking = True
            self._emit_state()
            vertex: str | None = None
            failure: BaseException | None = None
            try:
                vertex = await self._ask_move(engine, position_of(game), _letter(mover), sink)
            except Exception as exc:  # noqa: BLE001 - reported below, never escapes the task
                failure = exc
            finally:
                self.thinking = False
        if engine is not self.engine:
            # A superseded or lost engine: its failure or move is not news.
            self._emit_state()
            return "stale"
        if failure is not None:
            if not isinstance(failure, ConnectionClosed):
                if isinstance(failure, EngineError):
                    self._error(self._engine_failure("Engine move failed", failure))
                else:
                    self._error(f"Engine move failed unexpectedly ({type(failure).__name__})")
            self._emit_state()
            self._request_analysis()
            return "failed"
        current = self._slot(token[0])
        stale = (current is None or current.id != self.active_board
                 or current.version != token[1] or self._epoch != token[2]
                 or (not explicit and (self._players_epoch != token[3]
                                       or not self._engine_plays(mover))))
        if stale:
            self.status = f"Discarded the engine's {vertex}: the position changed"
            self._emit_state()
            self._request_analysis()
            return "stale"
        assert vertex is not None
        game = current.game
        note = "" if game.cursor == game.move_count else (
            f"Branched at move {game.cursor}; the later moves were discarded")
        if vertex == "resign":
            # A genmove at a past cursor branches, its `resign` answer included (§3.2): the
            # resignation ends the game that is on the board, not one with later moves.
            game.branch()
            game.resign(mover)
            resigns = f"{_colour_name(mover).capitalize()} resigns"
            self._position_changed(current, f"{note}. {resigns}" if note else resigns,
                                   rearm=False)
            return "played"
        try:
            game.play(mover, coords.from_gtp(vertex, game.size))
        except (IllegalMove, ValueError) as exc:
            self._error(f"The engine's move {vertex[:ECHO]} was refused: {str(exc)[:200]}")
            self._emit_state()
            self._request_analysis()
            return "failed"
        self._position_changed(current, note, rearm=False)
        return "played"

    # -- analysis (§3.2, §3.3) ---------------------------------------------------------------------
    def _analysis_sink(self, slot: BoardSlot, engine: Engine) -> Callable[[Analysis], None]:
        """A report consumer bound to one board position and one engine connection. It runs
        inside the engine's reader, so it only records and broadcasts -- never calls the engine."""
        board_id, version = slot.id, slot.version

        def sink(analysis: Analysis) -> None:
            target = self._slot(board_id)
            if (target is None or target.version != version or engine is not self.engine
                    or not self.play_settings["analysisEnabled"] or self._closed):
                return
            target.last_analysis = analysis
            target.analysis_version = version
            if target.id == self.active_board:
                self._emit(self._analysis_frame(target, analysis))

        return sink

    def _request_analysis(self) -> None:
        """(Re)start or stop analysis for the position on screen, behind any engine work.
        Requests are coalesced: one refresh task at most, which reads the latest request."""
        if self.engine is None or self._closed:
            return
        self._analysis_wanted = True
        task = self._analysis_task
        if task is None or task.done():
            self._analysis_task = self._spawn(self._refresh_analysis())

    def _wants_analysis(self) -> bool:
        if not self.play_settings["analysisEnabled"] or self.game.is_game_over():
            return False
        # An engine move is coming: it stops analysis anyway (§3.2).
        return not (self._auto_running() and self._engine_should_move())

    async def _refresh_analysis(self) -> None:
        while self._analysis_wanted:
            async with self._lock:
                if not self._analysis_wanted:
                    return
                self._analysis_wanted = False  # this pass reads the latest request
                engine = self.engine
                if engine is None or self._closed:
                    return
                slot = self._active
                try:
                    if self._wants_analysis():
                        await engine.start_analysis(
                            position_of(slot.game), self._analysis_sink(slot, engine),
                            max_visits=self.engine_settings["maxVisits"],
                            interval=self.engine_settings["reportInterval"],
                            include_ownership=self.engine_settings["includeOwnership"])
                    else:
                        await engine.stop_analysis()
                except EngineError as exc:
                    if engine is self.engine and not isinstance(exc, ConnectionClosed):
                        self._error(self._engine_failure("Analysis failed", exc))

    # -- the console and final score (§3.5) ----------------------------------------------------------
    def _msg_raw(self, message: dict) -> None:
        command = _string(message, "command")
        engine = self.engine
        if engine is None:
            raise _Refused("no engine is connected")
        if not engine.supports_raw or self._target is None or not self._target.console:
            raise _Refused("the console is not offered for this engine")
        if len(command) > MAX_RAW:
            raise _Refused(f"a console command is at most {MAX_RAW:,} characters")
        if any(c in command for c in "\r\n\0"):
            raise _Refused("a console command is one line")
        if not command.strip():
            raise _Refused("the console command is empty")
        if _is_streaming(command):
            raise _Refused("streaming commands cannot be sent from the console")
        self._on_demand("raw", self._raw(engine, command))

    async def _raw(self, engine: Engine, command: str) -> None:
        async with self._lock:
            if engine is not self.engine:
                return
            try:
                await engine.raw(command)  # type: ignore[attr-defined]
                failure = None
            except EngineError as exc:
                failure = exc
        if engine is not self.engine:
            return
        if failure is not None and not isinstance(failure, ConnectionClosed):
            name = command.split()[0] if command.split() else command
            self._error(self._engine_failure(name[:40], failure))
        # The engine's board may have moved: the position token changes and analysis restarts.
        self._active.changed()
        self._request_analysis()

    def _msg_final_score(self, message: dict) -> None:
        engine = self.engine
        if engine is None:
            raise _Refused("no engine is connected")
        if not engine.supports_final_score:
            raise _Refused("the engine cannot score the game")
        self._on_demand("final_score", self._final_score(engine))

    async def _final_score(self, engine: Engine) -> None:
        async with self._lock:
            if engine is not self.engine:
                return
            slot = self._active
            version = slot.version
            try:
                score = await engine.final_score(position_of(slot.game))  # type: ignore
                failure = None
            except EngineError as exc:
                failure = exc
        if engine is not self.engine:
            return
        if failure is not None:
            if not isinstance(failure, ConnectionClosed):
                self._error(self._engine_failure("final_score failed", failure))
        elif self._slot(slot.id) is slot and slot.version == version:
            text = " ".join(str(score).split())[:MAX_RESULT]
            if RESULT_PATTERN.fullmatch(text):
                slot.game.result = text
                self.status = f"The engine scores the game {text}"
            else:
                # Not an SGF result: recorded nowhere but the (scrubbed) status (§3.5, §7.7).
                self.status = f"The engine's score is not a game result: {text}"
            self._emit_state()
        self._request_analysis()

    # -- the engine connection (§3.2, §6.3) ------------------------------------------------------------
    def _connect_pending(self) -> bool:
        """A connect of the live lifecycle is pending; a superseded one no longer is (§4.1)."""
        return self._connecting is not None and self._connecting == self._lifecycle

    def _attempts_full(self) -> bool:
        """Connect attempts and engines still being closed share a bound of two; a new connect
        also releases the current engine, which then counts as one being closed (§3.2, §4.1)."""
        releasing = 1 if self.engine is not None else 0
        return len(self._attempts) + len(self._closing) + releasing >= MAX_CONNECT_ATTEMPTS

    def _msg_connect(self, message: dict) -> None:
        if self._connect_pending():
            raise _Refused("a connect is already in progress")
        if self._attempts_full():
            raise _Refused("a previous engine is still closing")
        target = self._resolve_target(message)
        self._start_connect(target)

    def _start_connect(self, target: EngineTarget) -> asyncio.Task:
        generation = self._begin_lifecycle()
        self._connecting = generation
        self._attempts.add(generation)

        async def run() -> None:
            try:
                await self._connect(target, generation)
            finally:
                self._attempts.discard(generation)
                if self._connecting == generation:
                    self._connecting = None

        return self._spawn(run())

    def _resolve_target(self, request: Any) -> EngineTarget:
        try:
            target = self._resolve(request)
        except EngineRequestError as exc:
            raise _Refused(exc.message or "the engine request was refused") from None
        except Exception:  # noqa: BLE001 - a policy bug must not leak its text
            raise _Refused("the engine request was refused") from None
        if not isinstance(target, EngineTarget) or not str(target.host).strip():
            raise _Refused("the engine request was refused")  # an empty host names no engine
        self._hidden.add((str(target.host), target.port))
        return target

    def _begin_lifecycle(self) -> int:
        """A new connect or disconnect: the latest wins, and the current engine is released."""
        self._lifecycle += 1
        self._detach_engine()
        return self._lifecycle

    def _detach_engine(self) -> None:
        engine, self.engine = self.engine, None
        if engine is not None:
            self.thinking = False
            self._spawn_close(engine)

    def _spawn_close(self, engine: Engine) -> None:
        """Close a released engine in the background; it counts against the bound until its
        close is over (§3.2)."""
        self._closing.add(engine)
        task = self._spawn(self._close_engine(engine))

        def done(task: asyncio.Task, engine: Engine = engine) -> None:
            # _close_engine discards it too; this covers a task cancelled before it ever ran
            self._closing.discard(engine)
            if task.cancelled():  # possibly before it ever ran: drop the connection
                _abort_engine(engine)

        task.add_done_callback(done)

    async def _close_engine(self, engine: Engine) -> None:
        """Close ``engine``; a close that fails, times out or is cancelled drops its connection
        at once rather than leaving it open (§3.2)."""
        engine.on_disconnect = None
        try:
            await asyncio.wait_for(engine.close(), CLOSE_TIMEOUT)
        except Exception:  # noqa: BLE001 - shutting it down anyway
            _abort_engine(engine)
        except BaseException:
            _abort_engine(engine)
            raise
        finally:
            self._closing.discard(engine)

    async def _connect(self, target: EngineTarget, generation: int) -> None:
        self.status = "Connecting to the engine"
        self._emit_state()
        engine: Engine | None = None
        try:
            engine = create_engine(target.protocol, target.host, target.port, self._record_log)
            engine.on_disconnect = lambda error, e=engine: self._engine_lost(e, error)
            self._configure_engine(engine)
            await engine.connect()
        except asyncio.CancelledError:
            if engine is not None:
                _abort_engine(engine)  # cut short (at shutdown): drop whatever it opened
            raise
        except Exception as exc:  # noqa: BLE001 - EngineError, or a bug in the client
            if engine is not None:
                await self._close_engine(engine)
            if generation == self._lifecycle and not self._closed:
                self._want_connected = False
                failure = exc if isinstance(exc, EngineError) else EngineError(
                    f"unexpected {type(exc).__name__}")
                if self.expose_address and failure.address is None:
                    failure.address = (target.host, target.port)
                self.status = self._engine_failure("Engine connection failed", failure)
                self._error(self.status)
                self._emit_state()
            return
        if generation != self._lifecycle or self._closed:
            await self._close_engine(engine)  # superseded: the latest lifecycle wins
            return
        self.engine = engine
        self._target = target
        self._request = copy.deepcopy(target.request_echo)
        self._shown_request = copy.deepcopy(target.request_echo)
        self._want_connected = True
        where = f" at {target.host}:{target.port}" if self.expose_address else ""
        self.status = f"Connected to {engine.description} ({engine.protocol}){where}"
        self._emit_state()
        self._maybe_engine_move()
        self._request_analysis()

    def _msg_disconnect(self, message: dict) -> None:
        had = self.engine is not None or self._connect_pending()
        self._begin_lifecycle()
        self._want_connected = False
        if had:
            self.status = "Engine disconnected"
        self._emit_state()

    def _engine_lost(self, engine: Engine, error: EngineError) -> None:
        """The connection was lost (§2.1): scheduled by the client, never awaited by its reader,
        and never takes the session lock."""
        if engine is not self.engine or self._closed:
            return
        self.engine = None
        self.thinking = False
        self._want_connected = False
        self.status = f"Engine disconnected: {error.message or 'the connection was lost'}"
        self._emit_state()
        self._spawn_close(engine)

    # -- persistence (§8.1) --------------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        return {
            "version": 1,
            "activeBoard": self.active_board,
            "boards": [{"id": s.id, "name": s.name, "sgf": s.game.to_sgf(),
                        "cursor": s.game.cursor, "humanProfile": s.profile,
                        "humanPolicy": copy.deepcopy(s.policy),
                        "humanCompare": copy.deepcopy(s.compare)} for s in self.boards],
            "engine": {"connected": self._want_connected,
                       "request": copy.deepcopy(self._request),
                       **{k: self.engine_settings[k] for k in DEFAULTS_ENGINE}},
            "play": dict(self.play_settings),
        }

    def restore(self, data: Any) -> None:
        """Load a snapshot forgivingly (§8.1); a version other than 1 raises ``ValueError`` and
        changes nothing."""
        if not isinstance(data, dict):
            raise ValueError("a snapshot must be an object")
        version = data.get("version")
        if type(version) is not int or version != 1:  # noqa: E721 - True is not version 1
            raise ValueError(f"unsupported snapshot version {str(version)[:20]!r}")
        engine_block = data.get("engine") if isinstance(data.get("engine"), dict) else {}
        play_block = data.get("play") if isinstance(data.get("play"), dict) else {}
        engine_settings = dict(DEFAULTS_ENGINE)
        for key, low, high in (("maxVisits", 1, MAX_VISITS), ("evalVisits", 0, MAX_VISITS)):
            value = engine_block.get(key)
            if type(value) is int and low <= value <= high:  # noqa: E721
                engine_settings[key] = value
        interval = engine_block.get("reportInterval")
        if _is_number(interval) and MIN_INTERVAL <= interval <= MAX_INTERVAL:
            engine_settings["reportInterval"] = interval
        if isinstance(engine_block.get("includeOwnership"), bool):
            engine_settings["includeOwnership"] = engine_block["includeOwnership"]
        play_settings = dict(DEFAULTS_PLAY)
        for key in ("blackIsEngine", "whiteIsEngine", "analysisEnabled"):
            if isinstance(play_block.get(key), bool):
                play_settings[key] = play_block[key]
        for key in ("blackStyle", "whiteStyle"):
            if play_block.get(key) in MOVE_STYLES:
                play_settings[key] = play_block[key]

        raw_boards = data.get("boards") if isinstance(data.get("boards"), list) else []
        slots: list[BoardSlot] = []
        wanted_ids: list[Any] = []
        for raw in [b for b in raw_boards if isinstance(b, dict)][:MAX_BOARDS]:
            slots.append(self._restore_board(raw, engine_settings["maxVisits"]))
            wanted_ids.append(raw.get("id"))
        taken: set[int] = set()
        for slot, wanted in zip(slots, wanted_ids):
            if type(wanted) is int and wanted > 0 and wanted not in taken:  # noqa: E721
                slot.id = wanted
                taken.add(wanted)
            else:
                slot.id = 0
        counter = itertools.count(max(taken, default=0) + 1)
        for slot in slots:
            if slot.id == 0:
                slot.id = next(counter)
                taken.add(slot.id)
            if not slot.name:
                slot.name = f"Board {slot.id}"
        if not slots:
            slots = [BoardSlot(1, "Board 1", Game(19))]
        active = data.get("activeBoard")
        if not any(type(active) is int and s.id == active for s in slots):  # noqa: E721
            active = slots[0].id
        request = _flat_request(engine_block.get("request"))

        self.boards = slots
        self.active_board = active
        self._board_ids = itertools.count(max(s.id for s in slots) + 1)
        self.engine_settings = engine_settings
        self.play_settings = play_settings
        self._request = request
        self._shown_request = None  # until the replay resolves (§4.2)
        self._want_connected = engine_block.get("connected") is True and request is not None
        self._epoch += 1
        self._configure_engine()
        self._emit_state()

    @staticmethod
    def _restore_board(raw: dict, max_visits: int) -> BoardSlot:
        game = Game(19)
        sgf = raw.get("sgf")
        if isinstance(sgf, str):
            try:
                if len(sgf.encode("utf-8", errors="surrogatepass")) <= MAX_SGF_BYTES:
                    game = Game.from_sgf(sgf)
            except Exception:  # noqa: BLE001 - a board that no longer reads comes back empty
                game = Game(19)
        cursor = raw.get("cursor")
        game.navigate(cursor if type(cursor) is int else game.move_count)  # noqa: E721
        name = raw.get("name")
        name = _trim(_trim(name)[:MAX_NAME]) if isinstance(name, str) else ""
        profile = raw.get("humanProfile")
        policy = raw.get("humanPolicy")
        compare = raw.get("humanCompare")
        return BoardSlot(
            0, name, game,
            profile if _profile_ok(profile) else DEFAULT_PROFILE,
            copy.deepcopy(policy) if tuple_problem(policy, max_visits) is None else {},
            None if compare is None else (
                copy.deepcopy(compare) if tuple_problem(compare, max_visits) is None else {}))

    async def resume(self) -> None:
        """Replay the stored engine request through the policy when the snapshot says the space
        was connected (§6.3, §8.1); a request the policy refuses leaves it disconnected."""
        if self._closed or not self._want_connected or self.engine is not None:
            return
        if self._connect_pending() or self._attempts_full():
            return
        try:
            target = self._resolve_target(copy.deepcopy(self._request))
        except _Refused as exc:
            # A refused request is dropped: from the snapshot and from state (§4.2, §8.1).
            self._want_connected = False
            self._request = None
            self._shown_request = None
            self.status = f"Engine not reconnected: {exc}"
            self._emit_state()
            return
        self._shown_request = copy.deepcopy(target.request_echo)
        await asyncio.shield(self._start_connect(target))

    # -- shutdown ------------------------------------------------------------------------------------
    async def aclose(self) -> None:
        """Release the engine and every session task. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._lifecycle += 1
        engine, self.engine = self.engine, None
        try:
            if engine is not None:
                await self._close_engine(engine)
            tasks = [t for t in self._tasks if not t.done()]
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=2.0)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        except BaseException:
            # aclose itself was cancelled: leave nothing running and no engine still closing
            for task in list(self._tasks):
                task.cancel()
            for closing in list(self._closing):
                _abort_engine(closing)
            raise
        finally:
            self._tasks.clear()

    _HANDLERS: dict[str, Callable[["GameSession", dict], Any]] = {
        "play": _msg_play,
        "pass": _msg_pass,
        "resign": _msg_resign,
        "undo": _msg_undo,
        "navigate": _msg_navigate,
        "new_game": _msg_new_game,
        "genmove": _msg_genmove,
        "connect": _msg_connect,
        "disconnect": _msg_disconnect,
        "analysis": _msg_analysis,
        "players": _msg_players,
        "engine_params": _msg_engine_params,
        "human_params": _msg_human_params,
        "board_select": _msg_board_select,
        "board_delete": _msg_board_delete,
        "board_duplicate": _msg_board_duplicate,
        "board_new": _msg_board_new,
        "board_move": _msg_board_move,
        "board_rename": _msg_board_rename,
        "raw": _msg_raw,
        "final_score": _msg_final_score,
        "preferences": _msg_preferences,
        "load_sgf": _msg_load_sgf,
        "state": _msg_state,
    }
