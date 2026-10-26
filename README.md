# Conveyor Clash

A real-time warehouse guessing game for 2 to 6 players. Every round, five packages roll down a conveyor belt. Everyone secretly picks one. If two or more people grab the same package, it clashes and nobody scores it. Pick the package nobody else picks.

Built with Python (FastAPI and WebSockets). Friends join from any phone or laptop with a 4-letter room code. No accounts and nothing to install.

## How to play

1. One person creates a room and shares the code or invite link.
2. Everyone else joins. The host starts the game (2 players minimum).
3. Each round, pick one package and tap Lock in. Nothing is revealed until everyone has locked in, or 20 seconds pass.
4. A package picked by only you scores its points. A package picked by two or more players clashes and scores for nobody.
5. Forklift, once per game: grab two packages in a round. It only pays if neither one clashes.
6. Fragile packages are worth -4. Alone on one, you lose points. A clash on it is harmless. If you run out of time, you stumble into it.
7. Eight rounds. The last round is worth double. Highest score wins.

Keyboard shortcuts: 1 to 5 pick a package, F toggles the forklift, Enter locks in.

## Run it on your computer

**Easiest (Windows):** double-click `run.bat`. The first time it sets itself up (about a minute), then your browser opens the game. Keep the black window open while you play and close it to stop. On Mac or Linux, run `bash run.sh` instead. Needs Python 3.10 or newer.

**Manual way:**

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000, enter a name and create a room.

To play with friends on the same Wi-Fi, share `http://<your-computer-ip>:8000` (`run.bat` already allows this).

### Playing alone with bots

In the lobby, the host clicks **Add a bot player** (up to 5 bots). Bots pick on their own, so you can play a full game with one device. Remove a bot with the Remove button next to it.

`tools/bots.py` is an older alternative that adds bots from a terminal, including to a deployed site. You don't need it.

### Quick settings (optional)

Set these environment variables to change the pace, for example to test faster:

| Variable | Default | Meaning |
|----------|---------|---------|
| `CC_ROUND_SECONDS` | 20 | Time to pick each round |
| `CC_REVEAL_SECONDS` | 8 | How long results stay on screen |
| `CC_TOTAL_ROUNDS` | 8 | Rounds per game (the last one is always double) |

## Run the tests

```bash
pip install -r requirements-dev.txt
pytest
```

50 tests cover the scoring rules (clashes, forklift, fragile, double round) and real WebSocket games: joining, hidden picks, timeouts, reconnecting, rematch, and bad input.

## Put it online (free)

The game must be reachable at a public URL. Rooms live in memory, so always run a single worker (the config files already do).

### Option A: Render (easiest)

1. Create a free account at render.com and push this folder to a GitHub repository.
2. In Render choose New, then Blueprint, and select the repository. It reads `render.yaml` automatically.
   (Or choose New, then Web Service, with build command `pip install -r requirements.txt` and start command `uvicorn app.main:app --host 0.0.0.0 --port $PORT --workers 1`.)
3. Wait for the deploy. Your game is live at `https://<name>.onrender.com`.

Good to know on the free plan: the site sleeps after about 15 minutes without visitors, so the first load can take up to a minute. Open it once before you invite people. A restart also clears any running games.

### Option B: Railway, Fly.io, Hugging Face Spaces or any Docker host

The included `Dockerfile` works as is. It listens on `$PORT` (default 8000).

```bash
docker build -t conveyor-clash .
docker run -p 8000:8000 conveyor-clash
```

### Option C: Quick public link from your own computer

While `uvicorn` is running, a tunnel gives you a temporary public URL, for example `cloudflared tunnel --url http://localhost:8000` or `ngrok http 8000`. Good for a demo; the link stops working when you close your computer.

## Project layout

```
app/
  engine.py       Game rules and scoring (pure Python, no I/O)
  rooms.py        Rooms, players, round timer, reconnecting, cleanup
  main.py         FastAPI app: create room, WebSocket endpoint, static files
  static/         index.html, style.css, app.js (no build step)
tests/            Rules tests and end-to-end WebSocket tests
tools/bots.py     Optional terminal bots (the lobby has an Add bot button)
run.bat / run.sh  One-click start
docs/REQUIREMENTS.md   Requirements, rules and acceptance checks
```

## How the live sync works

The server owns all game state. Every time anything changes (a player joins, someone locks in, the timer ends), it sends each player a fresh snapshot of the room over their WebSocket, and the page redraws from it. A player's pick stays on the server until the reveal, so it cannot be peeked at.

Each player gets a private token saved in the browser. If a phone sleeps or the page refreshes, the page reconnects with the token and returns to the same seat.

## Limits and ideas

- Everything is in memory: one server process, rooms are deleted after 10 minutes with nobody connected.
- To scale to several servers you would move room state to Redis and use sticky routing.
- Ideas: spectators, custom round counts in the lobby, sound effects, a live "most contested package" stat.
