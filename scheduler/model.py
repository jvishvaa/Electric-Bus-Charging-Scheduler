"""
Pure data shapes for the bus charging scheduler.

No logic, no I/O. The scenario JSON parses into these; the solver consumes
these; the UI renders these. Adding a new field here is the *only* place that
changes when the world grows (new endpoint, new station, new bus property).
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal


# ──────────────────────────────────────────────
# Direction is stored verbatim from the scenario JSON.
# Two values are used today; the type is open so a future scenario could
# introduce a third route direction without a code change.
# ──────────────────────────────────────────────
Direction = str   # e.g. "Bengaluru->Kochi", "Kochi->Bengaluru"


@dataclass(frozen=True)
class Segment:
    """One leg of the route between two adjacent nodes."""
    from_node: str
    to_node: str
    distance_km: int


@dataclass(frozen=True)
class StationConfig:
    """A scheduling station — one of the inner nodes where charging happens."""
    name: str
    chargers: int   # how many buses can charge simultaneously


@dataclass(frozen=True)
class Route:
    """
    The physical road graph.

    `nodes` is the ordered list of endpoints + stations from one end to the
    other (e.g. ("Bengaluru","A","B","C","D","Kochi")). `endpoints` are the
    full-charge depots — buses begin and end there but do not schedule there.
    `stations` derives from nodes minus endpoints.
    """
    nodes: tuple[str, ...]
    segments: tuple[Segment, ...]
    endpoints: tuple[str, str]   # (forward_origin, forward_destination)

    @property
    def stations(self) -> tuple[str, ...]:
        return tuple(n for n in self.nodes if n not in self.endpoints)

    def path(self, direction: Direction) -> tuple[str, ...]:
        """Ordered nodes a bus visits given its direction string."""
        forward = f"{self.endpoints[0]}->{self.endpoints[1]}"
        if direction == forward:
            return self.nodes
        return tuple(reversed(self.nodes))

    def distance_between(self, a: str, b: str) -> int:
        """
        Distance from node `a` to node `b` along the route, regardless of which
        side you're starting from. Sums segment distances between them.
        """
        idx = {n: i for i, n in enumerate(self.nodes)}
        i, j = idx[a], idx[b]
        if i > j:
            i, j = j, i
        return sum(s.distance_km for s in self.segments[i:j])


@dataclass(frozen=True)
class Weights:
    """
    Soft objective weights. Engineers tune these as field data accumulates.
    All values are floats; the solver scales to integers internally.
    """
    individual: float   # worst single-bus total wait (minimax)
    operator: float     # worst per-operator total wait (minimax — fairness)
    overall: float      # sum of all waits (network throughput)


@dataclass(frozen=True)
class Bus:
    """One bus that hasn't started its trip yet — only a departure plan."""
    id: str
    operator: str
    direction: Direction
    departure_min: int   # minutes after `Scenario.reference_time`
    priority_pass_wait_time: bool = False
    min_soc_limit: int = 30


@dataclass(frozen=True)
class Scenario:
    """
    The full input to the scheduler.

    All times are stored as integer minutes from `reference_time`. The
    reference time is purely a display anchor — the solver does not care
    what wall-clock time it represents.
    """
    name: str
    reference_time: str          # "HH:MM", anchor for all minute offsets
    battery_range_km: int
    charge_minutes: int
    speed_kmph: int
    route: Route
    stations: dict[str, StationConfig]
    weights: Weights
    buses: tuple[Bus, ...]
    raw: dict = field(default_factory=dict)

    def travel_min(self, distance_km: int) -> int:
        """Distance → travel minutes at the scenario's speed (rounded)."""
        return int(round(distance_km * 60 / self.speed_kmph))
