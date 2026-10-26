"""Conveyor Clash rules.

Pure functions and dataclasses, no I/O, so the whole rule set is easy to test.

The rules in one paragraph
--------------------------
Every round a belt carries 5 packages, each worth some points. All players pick
one package in secret. Picks are revealed together. A package picked by exactly
one player scores for that player. A package picked by two or more players
"clashes" and scores for nobody. Once per game a player may use the Forklift to
grab two packages, but it only pays if neither of them clashes. Some belts carry
a Fragile package worth negative points; a clash on it is harmless, a sole pick
costs you, and a player who runs out of time stumbles into it. The last round is
worth double.
"""

from __future__ import annotations

import random
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

TOTAL_ROUNDS = 8
PACKAGES_PER_ROUND = 5
FRAGILE_VALUE = -4
FRAGILE_CHANCE = 0.4
VALUE_RANGE = range(1, 10)
LABELS = ("Crate", "Pallet", "Barrel", "Parcel", "Toolbox", "Drum", "Sack", "Carton")


@dataclass(frozen=True)
class Package:
    id: int
    value: int
    label: str
    fragile: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "value": self.value, "label": self.label, "fragile": self.fragile}


@dataclass(frozen=True)
class Belt:
    round_no: int
    packages: tuple[Package, ...]
    multiplier: int = 1

    @property
    def fragile_id(self) -> Optional[int]:
        for package in self.packages:
            if package.fragile:
                return package.id
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "round": self.round_no,
            "multiplier": self.multiplier,
            "packages": [p.to_dict() for p in self.packages],
        }


@dataclass(frozen=True)
class PlayerOutcome:
    picks: tuple[int, ...]  # what the player ended up with (after any stumble)
    forklift: bool  # used the forklift this round
    idle: bool  # submitted nothing before the timer ran out
    stumbled: bool  # idle and forced onto the fragile package
    clashed: tuple[int, ...]  # picked packages that someone else also picked
    scored: tuple[int, ...]  # packages that actually paid out
    forklift_failed: bool  # forklift used but a clash cancelled it
    gain: int


@dataclass(frozen=True)
class RoundResult:
    belt: Belt
    outcomes: dict[str, PlayerOutcome]
    contested: tuple[int, ...]


def make_belt(round_no: int, total_rounds: int = TOTAL_ROUNDS, rng: Optional[random.Random] = None) -> Belt:
    """Build the belt for a round. Round 1 never has a fragile package."""
    rng = rng or random.Random()
    has_fragile = round_no > 1 and rng.random() < FRAGILE_CHANCE
    normal_count = PACKAGES_PER_ROUND - (1 if has_fragile else 0)
    items: list[tuple[int, bool]] = [(v, False) for v in rng.sample(list(VALUE_RANGE), normal_count)]
    if has_fragile:
        items.append((FRAGILE_VALUE, True))
    rng.shuffle(items)
    labels = rng.sample(LABELS, PACKAGES_PER_ROUND)
    packages = tuple(Package(i, value, labels[i], fragile) for i, (value, fragile) in enumerate(items))
    multiplier = 2 if round_no == total_rounds else 1
    return Belt(round_no=round_no, packages=packages, multiplier=multiplier)


def validate_pick(belt: Belt, picks: Any, forklift_available: bool) -> Optional[str]:
    """Return an error message for an invalid pick, or None if it is fine."""
    if not isinstance(picks, (list, tuple)) or not picks:
        return "Pick a package first."
    if len(picks) > 2:
        return "You can grab at most two packages."
    for value in picks:
        if isinstance(value, bool) or not isinstance(value, int):
            return "That package does not exist."
        if not 0 <= value < len(belt.packages):
            return "That package does not exist."
    if len(set(picks)) != len(picks):
        return "Pick two different packages."
    if len(picks) == 2 and not forklift_available:
        return "Your forklift is already used up."
    return None


def resolve(belt: Belt, picks: Mapping[str, Sequence[int]]) -> RoundResult:
    """Score a round. `picks` maps player id -> package ids (empty = ran out of time)."""
    fragile_id = belt.fragile_id
    final: dict[str, tuple[int, ...]] = {}
    idle: set[str] = set()
    stumbled: set[str] = set()

    for player_id, chosen in picks.items():
        chosen = tuple(chosen)
        if chosen:
            final[player_id] = chosen
            continue
        idle.add(player_id)
        if fragile_id is not None:
            final[player_id] = (fragile_id,)
            stumbled.add(player_id)
        else:
            final[player_id] = ()

    counts = Counter(i for ids in final.values() for i in ids)
    contested = tuple(sorted(i for i, c in counts.items() if c > 1))

    outcomes: dict[str, PlayerOutcome] = {}
    for player_id, ids in final.items():
        clashed = tuple(i for i in ids if counts[i] > 1)
        forklift = len(ids) == 2
        forklift_failed = forklift and bool(clashed)
        scored = () if forklift_failed else tuple(i for i in ids if counts[i] == 1)
        gain = sum(belt.packages[i].value for i in scored) * belt.multiplier
        outcomes[player_id] = PlayerOutcome(
            picks=ids,
            forklift=forklift,
            idle=player_id in idle,
            stumbled=player_id in stumbled,
            clashed=clashed,
            scored=scored,
            forklift_failed=forklift_failed,
            gain=gain,
        )
    return RoundResult(belt=belt, outcomes=outcomes, contested=contested)


def winners(scores: Mapping[str, int]) -> list[str]:
    """Ids of every player sharing the top score."""
    if not scores:
        return []
    top = max(scores.values())
    return [player_id for player_id, score in scores.items() if score == top]
