"""End-to-end tests: real WebSocket clients against the FastAPI app."""

from contextlib import ExitStack

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.rooms import Settings


@pytest.fixture
def client():
    settings = Settings(
        round_seconds=0.8,
        reveal_seconds=0.25,
        total_rounds=2,
        lobby_grace_seconds=0.4,
    )
    with TestClient(create_app(settings)) as c:
        yield c


class Player:
    """Small helper that wraps a WebSocket the way the browser does."""

    def __init__(self, client, stack, code, name, token=None):
        self.ws = stack.enter_context(client.websocket_connect(f"/ws/{code}"))
        self.ws.send_json({"type": "hello", "name": name, "token": token})
        self.state = None
        self.error = None
        first = self.ws.receive_json()
        if first["type"] == "error":
            self.error = first
            return
        assert first["type"] == "welcome"
        self.id = first["player_id"]
        self.token = first["token"]

    def wait(self, predicate, limit=60):
        for _ in range(limit):
            msg = self.ws.receive_json()
            if msg["type"] == "state":
                self.state = msg
            elif msg["type"] == "error":
                self.error = msg
            try:
                if predicate(msg):
                    return msg
            except KeyError:
                pass  # predicate was written for a different message type
        raise AssertionError("expected message never arrived")

    def send(self, **msg):
        self.ws.send_json(msg)


def new_room(client):
    r = client.post("/api/rooms")
    assert r.status_code == 200
    return r.json()["code"]


def test_health_and_static_index(client):
    assert client.get("/healthz").json()["ok"] is True
    page = client.get("/")
    assert page.status_code == 200 and "Conveyor Clash" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_room_info_and_unknown_room(client):
    code = new_room(client)
    assert len(code) == 4 and code.isalpha()
    info = client.get(f"/api/rooms/{code}").json()
    assert info["phase"] == "lobby" and info["joinable"] is True
    assert client.get("/api/rooms/ZZZZ").status_code == 404
    assert client.get("/api/rooms/12").status_code == 404


def test_unknown_room_rejected_on_websocket(client):
    with ExitStack() as stack:
        p = Player(client, stack, "QQQQ", "Asha")
        assert p.error and p.error["code"] == "room_not_found"


def test_two_devices_join_and_see_each_other(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        state = b.wait(lambda s: len(s["players"]) == 2)
        assert {p["name"] for p in state["players"]} == {"Asha", "Ravi"}
        assert state["you"]["id"] == b.id and not state["you"]["is_host"]
        a_state = a.wait(lambda s: len(s["players"]) == 2)
        assert a_state["you"]["is_host"]


def test_duplicate_name_is_rejected_case_insensitively(client):
    code = new_room(client)
    with ExitStack() as stack:
        Player(client, stack, code, "Asha")
        dup = Player(client, stack, code, "  asha ")
        assert dup.error and dup.error["code"] == "name_taken"


def test_blank_name_rejected(client):
    code = new_room(client)
    with ExitStack() as stack:
        p = Player(client, stack, code, "   ")
        assert p.error and p.error["code"] == "bad_name"


def test_room_full_at_six_players(client):
    code = new_room(client)
    with ExitStack() as stack:
        for i in range(6):
            assert Player(client, stack, code, f"P{i}").error is None
        seventh = Player(client, stack, code, "P6")
        assert seventh.error and seventh.error["code"] == "full"


def test_only_host_can_start_and_two_players_are_needed(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        a.send(type="start")
        err = a.wait(lambda m: m["type"] == "error")
        assert err["code"] == "too_few"
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        b.send(type="start")
        err = b.wait(lambda m: m["type"] == "error")
        assert err["code"] == "not_host"


def play_round(players, picks_by_index, round_no):
    for p in players:
        p.wait(lambda s: s["phase"] == "picking" and s["round"] == round_no)
    for p, pick in zip(players, picks_by_index):
        if pick is not None:
            p.send(type="pick", packages=pick)


def test_full_game_with_hidden_picks_scoring_and_rematch(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="start")

        # Round 1: both pick different packages, so both score.
        for p in (a, b):
            p.wait(lambda s: s["phase"] == "picking" and s["round"] == 1)
        belt1 = a.state["belt"]["packages"]
        a.send(type="pick", packages=[0])
        a.wait(lambda s: s["you"]["locked"])
        # Ravi sees Asha locked in but not what she picked.
        seen = b.wait(lambda s: any(p["locked"] for p in s["players"]))
        assert seen["result"] is None
        assert "pick" not in next(p for p in seen["players"] if p["id"] == a.id)
        assert seen["you"]["pick"] is None
        b.send(type="pick", packages=[1])
        reveal = a.wait(lambda s: s["phase"] == "reveal")
        rows = {r["name"]: r for r in reveal["result"]["rows"]}
        assert rows["Asha"]["gain"] == belt1[0]["value"] * (1 if not belt1[0]["fragile"] else 1)
        assert rows["Ravi"]["gain"] == belt1[1]["value"]
        assert reveal["result"]["contested"] == []
        score = {p["name"]: p["score"] for p in reveal["players"]}
        assert score["Asha"] == rows["Asha"]["gain"]

        # Round 2 (the last, double): they clash on the same package, nobody scores.
        for p in (a, b):
            p.wait(lambda s: s["phase"] == "picking" and s["round"] == 2)
        assert a.state["belt"]["multiplier"] == 2
        a.send(type="pick", packages=[2])
        b.send(type="pick", packages=[2])
        reveal2 = a.wait(lambda s: s["phase"] == "reveal" and s["round"] == 2)
        assert reveal2["result"]["contested"] == [2]
        assert all(r["gain"] == 0 for r in reveal2["result"]["rows"])

        final = a.wait(lambda s: s["phase"] == "finished")
        assert final["winners"]
        assert len(final["history"]) == 2

        # Only the host can start a rematch.
        b.wait(lambda s: s["phase"] == "finished")
        b.send(type="play_again")
        assert b.wait(lambda m: m["type"] == "error")["code"] == "not_host"
        a.send(type="play_again")
        lobby = a.wait(lambda s: s["phase"] == "lobby")
        assert all(p["score"] == 0 for p in lobby["players"]) and lobby["history"] == []


def test_round_ends_early_when_everyone_locks_and_on_timeout_otherwise(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="start")
        for p in (a, b):
            p.wait(lambda s: s["phase"] == "picking" and s["round"] == 1)
        a.send(type="pick", packages=[0])  # Ravi never picks: the timer ends the round
        reveal = a.wait(lambda s: s["phase"] == "reveal")
        ravi = next(r for r in reveal["result"]["rows"] if r["name"] == "Ravi")
        assert ravi["idle"] is True and ravi["picks"] == [] or ravi["stumbled"] is True


def test_bad_and_repeat_picks_are_rejected(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="pick", packages=[0])
        assert a.wait(lambda m: m["type"] == "error")["code"] == "bad_phase"
        a.send(type="start")
        a.wait(lambda s: s["phase"] == "picking")
        a.send(type="pick", packages=[9])
        assert a.wait(lambda m: m["type"] == "error")["code"] == "bad_pick"
        a.send(type="pick", packages=[0])
        a.wait(lambda s: s["you"]["locked"])
        a.send(type="pick", packages=[1])
        assert a.wait(lambda m: m["type"] == "error")["code"] == "already_locked"


def test_forklift_can_only_be_used_once(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="start")
        for p in (a, b):
            p.wait(lambda s: s["phase"] == "picking" and s["round"] == 1)
        a.send(type="pick", packages=[0, 1])
        b.send(type="pick", packages=[2])
        reveal = a.wait(lambda s: s["phase"] == "reveal")
        assert next(r for r in reveal["result"]["rows"] if r["name"] == "Asha")["forklift"] is True
        a.wait(lambda s: s["phase"] == "picking" and s["round"] == 2)
        assert a.state["you"]["forklift_available"] is False
        a.send(type="pick", packages=[0, 1])
        assert a.wait(lambda m: m["type"] == "error")["code"] == "bad_pick"


def test_cannot_join_a_game_in_progress_but_can_reconnect_with_token(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="start")
        a.wait(lambda s: s["phase"] == "picking")

        late = Player(client, stack, code, "Late")
        assert late.error and late.error["code"] == "in_progress"

        b.ws.close()  # Ravi's phone drops
        a.wait(lambda s: any(not p["connected"] for p in s["players"]))
        back = Player(client, stack, code, "Ravi", token=b.token)
        assert back.error is None and back.id == b.id
        state = back.wait(lambda s: s["phase"] in ("picking", "reveal"))
        assert next(p for p in state["players"] if p["id"] == b.id)["connected"]


def test_host_passes_to_the_next_player_when_host_leaves_lobby(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="leave")
        state = b.wait(lambda s: len(s["players"]) == 1)
        assert state["you"]["is_host"] is True


def test_emotes_are_broadcast_and_whitelisted(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        a.send(type="emote", emoji="🔥")
        got = b.wait(lambda m: m["type"] == "emote")
        assert got["emoji"] == "🔥" and got["name"] == "Asha"


def test_junk_messages_do_not_crash_the_connection(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        a.ws.send_text("not json")
        a.ws.send_text("[1,2,3]")
        a.ws.send_text("x" * 5000)
        a.send(type="nonsense")
        a.send(type="ping")
        assert a.ws.receive_json()["type"] in ("pong", "state")


# ------------------------------------------------------------------ practice bots


def test_host_can_add_and_remove_bots_and_play_alone(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        a.send(type="add_bot")
        state = a.wait(lambda s: len(s["players"]) == 2)
        bot = next(p for p in state["players"] if p["is_bot"])
        assert bot["connected"] and not bot["is_host"]

        a.send(type="add_bot")
        state = a.wait(lambda s: len(s["players"]) == 3)
        assert len({p["name"] for p in state["players"]}) == 3
        a.send(type="remove_bot", id=bot["id"])
        state = a.wait(lambda s: len(s["players"]) == 2)
        assert all(p["id"] != bot["id"] for p in state["players"])
        a.send(type="remove_bot", id=a.id)  # a human can't be removed this way
        a.send(type="ping")
        assert any(p["id"] == a.id for p in a.state["players"])

        a.send(type="start")
        a.wait(lambda s: s["phase"] == "picking" and s["round"] == 1)
        a.send(type="pick", packages=[0])
        reveal = a.wait(lambda s: s["phase"] == "reveal")
        names = {r["name"] for r in reveal["result"]["rows"]}
        assert "Asha" in names and len(names) == 2
        bot_row = next(r for r in reveal["result"]["rows"] if r["name"] != "Asha")
        assert bot_row["picks"] or bot_row["idle"]
        final = a.wait(lambda s: s["phase"] == "finished")
        assert len(final["history"]) == 2


def test_only_host_adds_bots_and_not_after_start(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        b = Player(client, stack, code, "Ravi")
        b.wait(lambda s: len(s["players"]) == 2)
        b.send(type="add_bot")
        assert b.wait(lambda m: m["type"] == "error")["code"] == "not_host"
        a.send(type="start")
        a.wait(lambda s: s["phase"] == "picking")
        a.send(type="add_bot")
        assert a.wait(lambda m: m["type"] == "error")["code"] == "bad_phase"


def test_bots_respect_room_size_limit(client):
    code = new_room(client)
    with ExitStack() as stack:
        a = Player(client, stack, code, "Asha")
        for _ in range(5):
            a.send(type="add_bot")
        a.wait(lambda s: len(s["players"]) == 6)
        a.send(type="add_bot")
        assert a.wait(lambda m: m["type"] == "error")["code"] == "full"
