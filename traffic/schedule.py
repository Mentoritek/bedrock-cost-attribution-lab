"""Deterministic call plan: same config + seed => identical plan (spec section 6.2)."""

from __future__ import annotations

import math
import random
from collections import Counter
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PlannedCall:
    index: int
    team: str
    feature: str


def team_counts(teams: list[dict[str, Any]], total: int) -> dict[str, int]:
    """Largest-remainder rounding of total x weight. Ties go to the earlier team in config."""
    exact = [(t["name"], total * float(t["weight"])) for t in teams]
    counts = {name: math.floor(x) for name, x in exact}
    leftover = total - sum(counts.values())
    order = sorted(range(len(exact)), key=lambda i: (-(exact[i][1] - math.floor(exact[i][1])), i))
    for i in order[:leftover]:
        counts[exact[i][0]] += 1
    return counts


def feature_counts(features: list[str], n: int) -> dict[str, int]:
    """Even split across features; the remainder goes to the first feature."""
    base, rem = divmod(n, len(features))
    counts = {f: base for f in features}
    counts[features[0]] += rem
    return counts


def build_plan(cfg: dict[str, Any], calls: int | None = None, seed: int | None = None) -> list[PlannedCall]:
    total = int(calls if calls is not None else cfg["traffic"]["callsPerRun"])
    rng_seed = int(seed if seed is not None else cfg["traffic"]["seed"])
    ordered: list[tuple[str, str]] = []
    per_team = team_counts(cfg["teams"], total)
    for team in cfg["teams"]:
        for feature, n in feature_counts(team["features"], per_team[team["name"]]).items():
            ordered.extend([(team["name"], feature)] * n)
    random.Random(rng_seed).shuffle(ordered)
    return [PlannedCall(i, team, feature) for i, (team, feature) in enumerate(ordered)]


def plan_table(plan: list[PlannedCall]) -> list[dict[str, Any]]:
    counts = Counter((c.team, c.feature) for c in plan)
    return [{"team": t, "feature": f, "calls": n} for (t, f), n in sorted(counts.items())]


def assign_users(plan: list[PlannedCall], users: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Gateway modes: each call takes the next user (round-robin) whose team matches."""
    by_team: dict[str, list[dict[str, Any]]] = {}
    for u in users:
        by_team.setdefault(u["team"], []).append(u)
    cursor: dict[str, int] = {}
    assigned = []
    for call in plan:
        pool = by_team.get(call.team)
        if not pool:
            raise ValueError(f"no gateway user configured for team {call.team!r}")
        i = cursor.get(call.team, 0)
        assigned.append(pool[i % len(pool)])
        cursor[call.team] = i + 1
    return assigned
