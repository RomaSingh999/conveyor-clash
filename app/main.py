"""FastAPI app: REST for creating rooms, one WebSocket per player, static frontend."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .rooms import GameError, Player, RoomManager, Settings

log = logging.getLogger("conveyor_clash")
STATIC_DIR = Path(__file__).parent / "static"
CODE_RE = re.compile(r"^[A-Za-z]{4}$")
MAX_MESSAGE_CHARS = 2000
HELLO_TIMEOUT_SECONDS = 10


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    manager = RoomManager(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        sweeper = asyncio.create_task(manager.run_sweeper())
        try:
            yield
        finally:
            sweeper.cancel()
            manager.shutdown()

    app = FastAPI(title="Conveyor Clash", lifespan=lifespan)
    app.state.manager = manager

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True, "rooms": len(manager.rooms)}

    @app.post("/api/rooms")
    async def create_room():
        try:
            room = manager.create()
        except GameError as error:
            return JSONResponse({"error": error.message}, status_code=503)
        return {"code": room.code}

    @app.get("/api/rooms/{code}")
    async def room_info(code: str):
        room = manager.get(code) if CODE_RE.match(code) else None
        if room is None:
            raise HTTPException(status_code=404, detail="Room not found")
        return {
            "code": room.code,
            "phase": room.phase,
            "players": len(room.players),
            "max_players": room.settings.max_players,
            "joinable": room.phase == "lobby" and len(room.players) < room.settings.max_players,
        }

    @app.get("/")
    async def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.websocket("/ws/{code}")
    async def play(ws: WebSocket, code: str):
        await ws.accept()
        room = manager.get(code) if CODE_RE.match(code) else None
        if room is None:
            await _reject(ws, "room_not_found", "That room does not exist. Check the code or create a new room.", 4404)
            return

        player: Optional[Player] = None
        try:
            hello = await _read_json(ws, timeout=HELLO_TIMEOUT_SECONDS)
            if not hello or hello.get("type") != "hello":
                await _reject(ws, "bad_request", "Expected a hello message.", 4400)
                return
            try:
                player, previous = room.attach(hello.get("name"), hello.get("token"), ws)
            except GameError as error:
                await _reject(ws, error.code, error.message, 4400)
                return
            if previous is not None and previous is not ws:
                try:
                    await previous.close(code=4000)
                except Exception:
                    pass

            await ws.send_json({"type": "welcome", "player_id": player.id, "token": player.token, "code": room.code})
            await room.broadcast()

            while True:
                msg = await _read_json(ws)
                if msg is None:
                    continue
                if msg.get("type") == "ping":
                    await ws.send_json({"type": "pong"})
                    continue
                try:
                    if await room.handle(player, msg) == "leave":
                        break
                except GameError as error:
                    await ws.send_json({"type": "error", "code": error.code, "message": error.message})
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except Exception:  # never let one bad client take the server down
            log.exception("websocket error in room %s", code)
        finally:
            if player is not None:
                await room.detach(player, ws)
            try:
                await ws.close()
            except Exception:
                pass

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


async def _reject(ws: WebSocket, code: str, message: str, close_code: int) -> None:
    try:
        await ws.send_json({"type": "error", "code": code, "message": message, "fatal": True})
        await ws.close(code=close_code)
    except Exception:
        pass


async def _read_json(ws: WebSocket, timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Read one JSON object. Returns None for junk so the caller can ignore it."""
    packet = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if packet["type"] == "websocket.disconnect":
        raise WebSocketDisconnect(packet.get("code", 1000))
    raw = packet.get("text")
    if raw is None or len(raw) > MAX_MESSAGE_CHARS:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


app = create_app()
