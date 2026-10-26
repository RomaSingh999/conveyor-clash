"""Rooms, players and the real-time game loop.

All state lives in memory, so run the server with a single worker process.
Everything runs on one asyncio event loop, which keeps the logic simple: a
handler mutates state, then broadcasts a personalised snapshot to every player.
"""

from __future__ import annotations

import asyncio
import os
import random
import secrets
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from . import engine

ROOM_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ"  # no I, L or O: easy to read aloud
EMOTES = ("😈", "😂", "🔥", "😱", "👏", "🤔")
MAX_NAME_LENGTH = 16
BOT_NAMES = ("Bolt", "Pallet Pete", "Forky", "Crate Kate", "Box Fox", "Tape")


class GameError(Exception):
    """An error that is safe to show to the player."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Settings:
    round_seconds: float = 20.0
    reveal_seconds: float = 8.0
    total_rounds: int = engine.TOTAL_ROUNDS
    min_players: int = 2
    max_players: int = 6
    lobby_grace_seconds: float = 20.0
    empty_room_ttl_seconds: float = 600.0
    max_rooms: int = 500

    @classmethod
    def from_env(cls) -> "Settings":
        def number(name: str, default: float) -> float:
            try:
                return float(os.environ.get(name, default))
            except ValueError:
                return default

        return cls(
            round_seconds=number("CC_ROUND_SECONDS", cls.round_seconds),
            reveal_seconds=number("CC_REVEAL_SECONDS", cls.reveal_seconds),
            total_rounds=int(number("CC_TOTAL_ROUNDS", cls.total_rounds)),
        )


def clean_name(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFKC", raw)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    text = " ".join(text.split())
    return text[:MAX_NAME_LENGTH].strip()


@dataclass(eq=False)
class Player:
    id: str
    name: str
    token: str
    score: int = 0
    forklift_used: bool = False
    connected: bool = False
    left: bool = False
    is_bot: bool = False
    ws: Any = None
    disconnected_at: Optional[float] = None
    pick: Optional[tuple[int, ...]] = None
    last_emote: float = 0.0
    send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class Room:
    def __init__(self, code: str, settings: Settings, rng: Optional[random.Random] = None):
        self.code = code
        self.settings = settings
        self.rng = rng or random.Random()
        self.players: dict[str, Player] = {}
        self.host_id: Optional[str] = None
        self.phase = "lobby"  # lobby | picking | reveal | finished
        self.round_no = 0
        self.belt: Optional[engine.Belt] = None
        self.deadline: Optional[float] = None
        self.phase_seconds: float = 0.0
        self.last_result: Optional[dict[str, Any]] = None
        self.history: list[dict[str, Any]] = []
        self.winner_ids: list[str] = []
        self.empty_since: Optional[float] = time.monotonic()
        self._task: Optional[asyncio.Task] = None
        self._bot_tasks: list[asyncio.Task] = []
        self._all_locked = asyncio.Event()

    # ------------------------------------------------------------------ players

    def connected_players(self) -> list[Player]:
        return [p for p in self.players.values() if p.connected]

    def humans_connected(self) -> list[Player]:
        return [p for p in self.players.values() if p.connected and not p.is_bot]

    def attach(self, name: Any, token: Any, ws: Any) -> tuple[Player, Any]:
        """Join the room or reconnect. Returns (player, previous websocket or None)."""
        player: Optional[Player] = None
        if isinstance(token, str) and token:
            for candidate in self.players.values():
                if not candidate.left and secrets.compare_digest(candidate.token, token):
                    player = candidate
                    break

        if player is None:
            if self.phase != "lobby":
                raise GameError("in_progress", "This game already started. Ask the host for a new room code.")
            clean = clean_name(name)
            if not clean:
                raise GameError("bad_name", "Enter a name to join.")
            if any(clean.casefold() == p.name.casefold() for p in self.players.values()):
                raise GameError("name_taken", "That name is taken in this room. Pick another one.")
            if len(self.players) >= self.settings.max_players:
                raise GameError("full", f"This room is full ({self.settings.max_players} players).")
            player = Player(id=uuid.uuid4().hex[:8], name=clean, token=secrets.token_urlsafe(16))
            self.players[player.id] = player

        previous = player.ws
        player.ws = ws
        player.connected = True
        player.disconnected_at = None
        self.empty_since = None
        self._ensure_host()
        return player, previous

    async def detach(self, player: Player, ws: Any) -> None:
        if player.ws is not ws:
            return  # a newer connection already replaced this one
        player.ws = None
        player.connected = False
        player.disconnected_at = time.monotonic()
        self._ensure_host()
        if not self.humans_connected():
            self.empty_since = time.monotonic()
        self._check_all_locked()
        await self.broadcast()

    def _ensure_host(self) -> None:
        host = self.players.get(self.host_id) if self.host_id else None
        if host is not None and host.connected:
            return
        for player in self.players.values():
            if player.connected and not player.is_bot:
                self.host_id = player.id
                return
        if host is None:
            self.host_id = next((p.id for p in self.players.values() if not p.is_bot), None)

    def drop_stale_lobby_players(self, now: float) -> bool:
        if self.phase != "lobby":
            return False
        stale = [
            p.id
            for p in self.players.values()
            if not p.connected
            and p.disconnected_at is not None
            and now - p.disconnected_at > self.settings.lobby_grace_seconds
        ]
        for player_id in stale:
            del self.players[player_id]
        if stale:
            self._ensure_host()
        return bool(stale)

    def is_abandoned(self, now: float) -> bool:
        if self.humans_connected() or self.empty_since is None:
            return False
        ttl = self.settings.empty_room_ttl_seconds if self.players else min(60.0, self.settings.empty_room_ttl_seconds)
        return now - self.empty_since > ttl

    def shutdown(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        for task in self._bot_tasks:
            task.cancel()
        self._bot_tasks = []

    # ----------------------------------------------------------------- messages

    async def handle(self, player: Player, msg: dict[str, Any]) -> Optional[str]:
        """Handle one client message. Returns "leave" when the player left."""
        kind = msg.get("type")
        if kind == "pick":
            await self._on_pick(player, msg.get("packages"))
        elif kind == "start":
            await self._on_start(player)
        elif kind == "play_again":
            await self._on_play_again(player)
        elif kind == "add_bot":
            await self._on_add_bot(player)
        elif kind == "remove_bot":
            await self._on_remove_bot(player, msg.get("id"))
        elif kind == "emote":
            await self._on_emote(player, msg.get("emoji"))
        elif kind == "leave":
            await self._on_leave(player)
            return "leave"
        return None

    async def _on_pick(self, player: Player, packages: Any) -> None:
        if self.phase != "picking" or self.belt is None:
            raise GameError("bad_phase", "Picks are closed right now.")
        if player.pick is not None:
            raise GameError("already_locked", "You already locked in this round.")
        problem = engine.validate_pick(self.belt, packages, not player.forklift_used)
        if problem:
            raise GameError("bad_pick", problem)
        player.pick = tuple(packages)
        self._check_all_locked()
        await self.broadcast()

    async def _on_start(self, player: Player) -> None:
        if player.id != self.host_id:
            raise GameError("not_host", "Only the host can start the game.")
        if self.phase != "lobby":
            raise GameError("bad_phase", "The game already started.")
        for stale in [p.id for p in self.players.values() if not p.connected]:
            del self.players[stale]
        if len(self.players) < self.settings.min_players:
            raise GameError("too_few", f"You need at least {self.settings.min_players} players to start.")
        self._reset_scores()
        self._task = asyncio.create_task(self._run())

    async def _on_add_bot(self, player: Player) -> None:
        if player.id != self.host_id:
            raise GameError("not_host", "Only the host can add practice bots.")
        if self.phase != "lobby":
            raise GameError("bad_phase", "Bots can only be added in the lobby.")
        if len(self.players) >= self.settings.max_players:
            raise GameError("full", f"This room is full ({self.settings.max_players} players).")
        taken = {p.name.casefold() for p in self.players.values()}
        name = next((n for n in BOT_NAMES if n.casefold() not in taken), None)
        if name is None:
            name = f"Bot {len(self.players) + 1}"
        bot = Player(id=uuid.uuid4().hex[:8], name=name, token=secrets.token_urlsafe(16), connected=True, is_bot=True)
        self.players[bot.id] = bot
        await self.broadcast()

    async def _on_remove_bot(self, player: Player, bot_id: Any) -> None:
        if player.id != self.host_id:
            raise GameError("not_host", "Only the host can remove bots.")
        if self.phase != "lobby":
            raise GameError("bad_phase", "Bots can only be removed in the lobby.")
        target = self.players.get(bot_id) if isinstance(bot_id, str) else None
        if target is None or not target.is_bot:
            return
        del self.players[target.id]
        await self.broadcast()

    async def _on_play_again(self, player: Player) -> None:
        if player.id != self.host_id:
            raise GameError("not_host", "Only the host can start a rematch.")
        if self.phase != "finished":
            raise GameError("bad_phase", "The game is not over yet.")
        for stale in [p.id for p in self.players.values() if not p.connected]:
            del self.players[stale]
        self._back_to_lobby()
        await self.broadcast()

    async def _on_emote(self, player: Player, emoji: Any) -> None:
        if emoji not in EMOTES:
            return
        now = time.monotonic()
        if now - player.last_emote < 1.2:
            return
        player.last_emote = now
        await self._send_all({"type": "emote", "from": player.id, "name": player.name, "emoji": emoji})

    async def _on_leave(self, player: Player) -> None:
        if self.phase in ("lobby", "finished"):
            self.players.pop(player.id, None)
        else:
            player.left = True
            player.token = secrets.token_urlsafe(16)  # old token no longer reconnects
        player.ws = None
        player.connected = False
        player.disconnected_at = time.monotonic()
        self._ensure_host()
        if not self.humans_connected():
            self.empty_since = time.monotonic()
        self._check_all_locked()
        await self.broadcast()

    # ---------------------------------------------------------------- game loop

    def _reset_scores(self) -> None:
        for player in self.players.values():
            player.score = 0
            player.forklift_used = False
            player.pick = None
        self.history = []
        self.last_result = None
        self.winner_ids = []
        self.round_no = 0
        self.belt = None

    def _back_to_lobby(self) -> None:
        self._reset_scores()
        self.phase = "lobby"
        self.deadline = None

    def _check_all_locked(self) -> None:
        if self.phase != "picking":
            return
        connected = self.connected_players()
        if not connected or all(p.pick is not None for p in connected):
            self._all_locked.set()

    async def _run(self) -> None:
        total = self.settings.total_rounds
        try:
            for round_no in range(1, total + 1):
                self._begin_round(round_no)
                await self.broadcast()
                try:
                    await asyncio.wait_for(self._all_locked.wait(), timeout=self.settings.round_seconds)
                except asyncio.TimeoutError:
                    pass
                if not self.humans_connected():
                    self._back_to_lobby()  # every human left: abandon this game
                    await self.broadcast()
                    return
                self._resolve_round()
                await self.broadcast()
                await asyncio.sleep(self.settings.reveal_seconds)
            self._finish()
            await self.broadcast()
        except asyncio.CancelledError:
            raise

    def _begin_round(self, round_no: int) -> None:
        self.round_no = round_no
        self.belt = engine.make_belt(round_no, self.settings.total_rounds, self.rng)
        self.phase = "picking"
        self.phase_seconds = self.settings.round_seconds
        self.deadline = time.monotonic() + self.settings.round_seconds
        self.last_result = None
        for player in self.players.values():
            player.pick = None
        self._all_locked.clear()
        for bot in (p for p in self.players.values() if p.is_bot):
            self._bot_tasks.append(asyncio.create_task(self._bot_turn(bot, round_no)))
        self._bot_tasks = [t for t in self._bot_tasks if not t.done()]
        self._check_all_locked()

    async def _bot_turn(self, bot: Player, round_no: int) -> None:
        """A practice bot thinks for a bit, then picks at random."""
        await asyncio.sleep(self.rng.uniform(0.15, 0.6) * self.settings.round_seconds)
        if self.phase != "picking" or self.round_no != round_no or bot.pick is not None or self.belt is None:
            return
        ids = [p.id for p in self.belt.packages]
        if not bot.forklift_used and self.rng.random() < 0.15:
            bot.pick = tuple(self.rng.sample(ids, 2))
        else:
            bot.pick = (self.rng.choice(ids),)
        self._check_all_locked()
        await self.broadcast()

    def _resolve_round(self) -> None:
        assert self.belt is not None
        picks = {p.id: (p.pick or ()) for p in self.players.values()}
        result = engine.resolve(self.belt, picks)
        rows = []
        for player in self.players.values():
            outcome = result.outcomes[player.id]
            player.score += outcome.gain
            if outcome.forklift:
                player.forklift_used = True
            rows.append(
                {
                    "id": player.id,
                    "name": player.name,
                    "picks": list(outcome.picks),
                    "forklift": outcome.forklift,
                    "forklift_failed": outcome.forklift_failed,
                    "idle": outcome.idle,
                    "stumbled": outcome.stumbled,
                    "clashed": list(outcome.clashed),
                    "scored": list(outcome.scored),
                    "gain": outcome.gain,
                    "total": player.score,
                }
            )
        self.last_result = {
            "round": self.round_no,
            "multiplier": self.belt.multiplier,
            "packages": [p.to_dict() for p in self.belt.packages],
            "contested": list(result.contested),
            "rows": rows,
        }
        self.history.append(self.last_result)
        self.phase = "reveal"
        self.phase_seconds = self.settings.reveal_seconds
        self.deadline = time.monotonic() + self.settings.reveal_seconds

    def _finish(self) -> None:
        self.phase = "finished"
        self.deadline = None
        self.winner_ids = engine.winners({p.id: p.score for p in self.players.values()})

    # ------------------------------------------------------------ broadcasting

    def state_for(self, viewer: Player) -> dict[str, Any]:
        remaining_ms = None
        if self.deadline is not None and self.phase in ("picking", "reveal"):
            remaining_ms = max(0, int((self.deadline - time.monotonic()) * 1000))
        showing_result = self.phase in ("reveal", "finished")
        return {
            "type": "state",
            "code": self.code,
            "phase": self.phase,
            "round": self.round_no,
            "total_rounds": self.settings.total_rounds,
            "belt": self.belt.to_dict() if self.belt else None,
            "remaining_ms": remaining_ms,
            "phase_ms": int(self.phase_seconds * 1000),
            "min_players": self.settings.min_players,
            "max_players": self.settings.max_players,
            "players": [
                {
                    "id": p.id,
                    "name": p.name,
                    "score": p.score,
                    "connected": p.connected,
                    "left": p.left,
                    "locked": p.pick is not None,
                    "forklift_available": not p.forklift_used,
                    "is_host": p.id == self.host_id,
                    "is_bot": p.is_bot,
                }
                for p in self.players.values()
            ],
            "you": {
                "id": viewer.id,
                "locked": viewer.pick is not None,
                "pick": list(viewer.pick) if viewer.pick is not None else None,
                "forklift_available": not viewer.forklift_used,
                "is_host": viewer.id == self.host_id,
            },
            "result": self.last_result if showing_result else None,
            "history": self.history,
            "winners": self.winner_ids if self.phase == "finished" else [],
        }

    async def _send(self, player: Player, payload: dict[str, Any]) -> None:
        ws = player.ws
        if ws is None:
            return
        async with player.send_lock:
            try:
                await ws.send_json(payload)
            except Exception:
                pass  # the receive loop notices a dead socket and cleans up

    async def broadcast(self) -> None:
        targets = [p for p in self.players.values() if p.ws is not None]
        if targets:
            await asyncio.gather(*(self._send(p, self.state_for(p)) for p in targets))

    async def _send_all(self, payload: dict[str, Any]) -> None:
        targets = [p for p in self.players.values() if p.ws is not None]
        if targets:
            await asyncio.gather(*(self._send(p, payload) for p in targets))


class RoomManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.rooms: dict[str, Room] = {}

    def create(self) -> Room:
        if len(self.rooms) >= self.settings.max_rooms:
            raise GameError("busy", "The server is full right now. Try again in a few minutes.")
        for _ in range(100):
            code = "".join(secrets.choice(ROOM_CODE_ALPHABET) for _ in range(4))
            if code not in self.rooms:
                room = Room(code, self.settings)
                self.rooms[code] = room
                return room
        raise GameError("busy", "Could not create a room. Try again.")

    def get(self, code: str) -> Optional[Room]:
        return self.rooms.get(code.strip().upper())

    async def sweep_once(self) -> None:
        now = time.monotonic()
        for code, room in list(self.rooms.items()):
            changed = room.drop_stale_lobby_players(now)
            if room.is_abandoned(now):
                room.shutdown()
                del self.rooms[code]
            elif changed:
                await room.broadcast()

    async def run_sweeper(self, interval: float = 2.0) -> None:
        while True:
            await asyncio.sleep(interval)
            await self.sweep_once()

    def shutdown(self) -> None:
        for room in self.rooms.values():
            room.shutdown()
