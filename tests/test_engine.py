import random

import pytest

from app import engine
from app.engine import Belt, Package, resolve, validate_pick, winners


def belt(values, fragile=None, multiplier=1):
    """Build a belt from plain values. `fragile` is the index of the fragile package."""
    packages = tuple(
        Package(i, v, f"Box{i}", fragile=(i == fragile)) for i, v in enumerate(values)
    )
    return Belt(round_no=2, packages=packages, multiplier=multiplier)


# ---------------------------------------------------------------- belt generation


def test_belt_shape_and_rules():
    rng = random.Random(1)
    for round_no in range(1, 9):
        b = engine.make_belt(round_no, 8, rng)
        assert len(b.packages) == 5
        assert [p.id for p in b.packages] == [0, 1, 2, 3, 4]
        normal = [p.value for p in b.packages if not p.fragile]
        assert len(set(normal)) == len(normal)
        assert all(1 <= v <= 9 for v in normal)
        fragile = [p for p in b.packages if p.fragile]
        assert len(fragile) <= 1
        assert all(p.value == engine.FRAGILE_VALUE for p in fragile)
        assert len({p.label for p in b.packages}) == 5


def test_round_one_never_fragile_and_last_round_doubles():
    rng = random.Random(7)
    assert all(engine.make_belt(1, 8, rng).fragile_id is None for _ in range(200))
    assert engine.make_belt(8, 8, rng).multiplier == 2
    assert engine.make_belt(7, 8, rng).multiplier == 1


def test_fragile_shows_up_sometimes():
    rng = random.Random(3)
    seen = sum(engine.make_belt(3, 8, rng).fragile_id is not None for _ in range(300))
    assert 60 < seen < 180  # roughly 40%


# ------------------------------------------------------------------------ scoring


def test_sole_picks_score_and_clashes_do_not():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (0,), "b": (0,), "c": (1,)})
    assert r.contested == (0,)
    assert r.outcomes["a"].gain == 0 and r.outcomes["a"].clashed == (0,)
    assert r.outcomes["b"].gain == 0
    assert r.outcomes["c"].gain == 5 and r.outcomes["c"].scored == (1,)


def test_three_way_clash_scores_nothing():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (2,), "b": (2,), "c": (2,)})
    assert all(o.gain == 0 for o in r.outcomes.values())


def test_forklift_pays_both_when_neither_clashes():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (0, 1), "b": (2,)})
    assert r.outcomes["a"].forklift and not r.outcomes["a"].forklift_failed
    assert r.outcomes["a"].gain == 14
    assert r.outcomes["b"].gain == 3


def test_forklift_jams_if_either_package_clashes_and_still_blocks_others():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (0, 1), "b": (1,)})
    assert r.outcomes["a"].forklift_failed and r.outcomes["a"].gain == 0
    assert r.outcomes["a"].scored == ()
    assert r.outcomes["b"].gain == 0  # b clashed with the forklift pick
    assert r.contested == (1,)


def test_two_forklifts_can_both_jam():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (0, 1), "b": (1, 2)})
    assert r.outcomes["a"].gain == 0 and r.outcomes["b"].gain == 0


def test_double_round_doubles_gains():
    b = belt([9, 5, 3, 2, 1], multiplier=2)
    r = resolve(b, {"a": (0,), "b": (1, 2)})
    assert r.outcomes["a"].gain == 18
    assert r.outcomes["b"].gain == 16


# ----------------------------------------------------------------------- fragile


def test_sole_pick_on_fragile_costs_points():
    b = belt([9, 5, 3, 2, -4], fragile=4)
    r = resolve(b, {"a": (4,), "b": (0,)})
    assert r.outcomes["a"].gain == -4
    assert r.outcomes["b"].gain == 9


def test_clash_on_fragile_is_safe():
    b = belt([9, 5, 3, 2, -4], fragile=4)
    r = resolve(b, {"a": (4,), "b": (4,)})
    assert r.outcomes["a"].gain == 0 and r.outcomes["b"].gain == 0


def test_idle_player_stumbles_into_fragile():
    b = belt([9, 5, 3, 2, -4], fragile=4)
    r = resolve(b, {"a": (), "b": (0,)})
    assert r.outcomes["a"].idle and r.outcomes["a"].stumbled
    assert r.outcomes["a"].picks == (4,)
    assert r.outcomes["a"].gain == -4


def test_two_idle_players_clash_on_fragile_safely():
    b = belt([9, 5, 3, 2, -4], fragile=4)
    r = resolve(b, {"a": (), "b": (), "c": (0,)})
    assert r.outcomes["a"].gain == 0 and r.outcomes["b"].gain == 0


def test_idle_player_with_no_fragile_scores_zero_and_blocks_nothing():
    b = belt([9, 5, 3, 2, 1])
    r = resolve(b, {"a": (), "b": (0,)})
    assert r.outcomes["a"].idle and not r.outcomes["a"].stumbled
    assert r.outcomes["a"].picks == () and r.outcomes["a"].gain == 0
    assert r.outcomes["b"].gain == 9


def test_idle_stumble_can_clash_with_a_deliberate_fragile_pick():
    b = belt([9, 5, 3, 2, -4], fragile=4)
    r = resolve(b, {"a": (), "b": (4,)})
    assert r.outcomes["a"].gain == 0 and r.outcomes["b"].gain == 0


# --------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "picks,forklift,ok",
    [
        ([0], True, True),
        ([0], False, True),
        ([0, 1], True, True),
        ([0, 1], False, False),  # forklift already used
        ([0, 0], True, False),
        ([0, 1, 2], True, False),
        ([], True, False),
        (None, True, False),
        ("0", True, False),
        ([5], True, False),
        ([-1], True, False),
        ([True], True, False),
        (["1"], True, False),
        ([1.5], True, False),
    ],
)
def test_validate_pick(picks, forklift, ok):
    b = belt([9, 5, 3, 2, 1])
    assert (validate_pick(b, picks, forklift) is None) == ok


# ------------------------------------------------------------------------ winners


def test_winners_and_ties():
    assert winners({"a": 3, "b": 9, "c": 5}) == ["b"]
    assert winners({"a": 9, "b": 9, "c": 5}) == ["a", "b"]
    assert winners({}) == []
    assert winners({"a": -2}) == ["a"]


# ------------------------------------------------------------- random invariants


def test_random_rounds_never_double_pay_a_package():
    rng = random.Random(11)
    for _ in range(500):
        b = engine.make_belt(rng.randint(1, 8), 8, rng)
        picks = {}
        for pid in "abcdef":
            choice = rng.random()
            if choice < 0.1:
                picks[pid] = ()
            elif choice < 0.3:
                picks[pid] = tuple(rng.sample(range(5), 2))
            else:
                picks[pid] = (rng.randrange(5),)
        r = resolve(b, picks)
        paid = [i for o in r.outcomes.values() for i in o.scored]
        assert len(paid) == len(set(paid))
        assert not set(paid) & set(r.contested)
        for o in r.outcomes.values():
            expected = sum(b.packages[i].value for i in o.scored) * b.multiplier
            assert o.gain == expected
