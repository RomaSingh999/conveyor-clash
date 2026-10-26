"""Fake players for testing Conveyor Clash on your own.

Create a room in your browser, then add bots to it:

    python tools/bots.py --url http://localhost:8000 --code ABCD --count 2

Bots pick packages at random (and sometimes use their forklift), so you can try
the whole game with only one real device. Needs:  pip install websockets
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from urllib.parse import urlparse

import websockets

NAMES = ["Bolt", "Pallet Pete", "Forky", "Crate Kate", "Box Fox", "Tape"]


async def run_bot(ws_url: str, name: str, rng: random.Random, verbose: bool) -> None:
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"type": "hello", "name": name}))
        picked_round = 0
        forklift_available = True
        async for raw in ws:
            msg = json.loads(raw)
            if msg["type"] == "error":
                print(f"[{name}] {msg['message']}")
                if msg.get("fatal"):
                    return
                continue
            if msg["type"] != "state":
                continue
            forklift_available = msg["you"]["forklift_available"]
            if msg["phase"] == "picking" and not msg["you"]["locked"] and msg["round"] != picked_round:
                picked_round = msg["round"]
                await asyncio.sleep(rng.uniform(0.8, 4.0))  # look human
                ids = [p["id"] for p in msg["belt"]["packages"]]
                if forklift_available and rng.random() < 0.15:
                    choice = rng.sample(ids, 2)
                else:
                    choice = [rng.choice(ids)]
                await ws.send(json.dumps({"type": "pick", "packages": choice}))
                if verbose:
                    print(f"[{name}] round {picked_round}: picked {choice}")
            if msg["phase"] == "finished":
                print(f"[{name}] game over")
                return


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://localhost:8000", help="server address, e.g. https://my-app.onrender.com")
    parser.add_argument("--code", required=True, help="room code shown in your browser")
    parser.add_argument("--count", type=int, default=2, help="how many bots to add (default 2)")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    parsed = urlparse(args.url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = f"{scheme}://{parsed.netloc}/ws/{args.code.upper()}"
    rng = random.Random(args.seed)
    names = NAMES[: max(0, min(args.count, len(NAMES)))]
    await asyncio.gather(*(run_bot(ws_url, n, random.Random(rng.random()), args.verbose) for n in names))


if __name__ == "__main__":
    asyncio.run(main())
