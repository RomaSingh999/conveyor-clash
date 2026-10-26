# Conveyor Clash: requirements and plan

Written before building, so the build could be checked against it.

## Goal

A multiplayer game that friends, family or classmates can join from their own devices, with every screen kept in sync, live at a public URL.

## Must-have requirements (from the challenge brief)

| # | Requirement | How Conveyor Clash meets it |
|---|-------------|-----------------------------|
| 1 | At least two players can join from separate devices | Room code or invite link, 2 to 6 players, each on their own phone or laptop |
| 2 | Real rules a player can follow without help | One-screen rules in the lobby, a "How to play" button everywhere, and a status line that tells you what to do each moment |
| 3 | Live at a URL other people can reach | Deploy files for Render, Docker and any Procfile host (see README) |
| 4 | Every player's screen stays in sync | The server owns all state and pushes a full snapshot over WebSockets after every change |

## Game rules (final)

- 2 to 6 players, 8 rounds.
- Each round the belt shows 5 packages worth 1 to 9 points. All players pick one in secret.
- All picks are revealed together. A package picked by exactly one player scores for them. A package picked by two or more clashes and scores for nobody.
- Forklift, once per game: pick two packages. It pays only if neither one clashes.
- Fragile package (about 40 percent of rounds after round 1): worth -4. Alone on it, you lose 4. A clash on it is harmless. If you run out of time, you stumble into it.
- A round ends when everyone has locked in, or after 20 seconds.
- The last round is worth double. Highest total wins, ties are shared.

## Non-goals

No accounts, no database, no chat text (reactions only), no spectators. Rooms are temporary and live in memory.

## Decisions

- Python backend: FastAPI with WebSockets, one process, in-memory rooms.
- Plain HTML, CSS and JS frontend, no build step, served by the same app.
- Picks stay on the server until the reveal, so nobody can peek by inspecting network traffic.
- Reconnecting: each player gets a private token saved in the browser, so a refresh or a dropped phone connection returns them to the same seat.

## Acceptance checks

1. Two browsers join one room and both see the player list update.
2. A pick by one player shows as "Locked in" to the other, without revealing which package.
3. A same-package pick shows Clash and scores zero; a unique pick scores.
4. The forklift works once, then is disabled.
5. Refreshing the page mid-game returns you to the game.
6. A game with 3 players runs from lobby to final screen and can restart.

Checks 1 to 4 and 6 are covered by `tests/test_server.py`; the rules by `tests/test_engine.py`.
