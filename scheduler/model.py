"""
model.py — Pure data model. No logic here. Just typed containers.

WHY THIS FILE EXISTS:
  The solver, loader, and UI all need to agree on what a "Bus" or "Scenario"
  looks like. Putting it here means changing a field in one place propagates
  everywhere. If the interviewer asks to add a new field (e.g. bus priority),
  this is the first file to touch.

DESIGN DECISIONS:
  - All times internally are "minutes from snapshot_time". HH:MM strings exist
    only in the JSON file and the UI. Internally we work in integers — CP-SAT
    requires integers, and arithmetic is simpler.
  - frozen=True on dataclasses means these objects are immutable (hashable,
    safe to use as dict keys). The solver never mutates input data.
  - 'raw' dict on Scenario stores the original JSON so the UI can show it
    without re-reading the file.

INTERVIEW HOT SPOTS:
  - "Add a field to Bus" → add it here, then update loader.py and
    optionally solver.py if the solver should use it.
  - "What is cumulative_wait_min?" → it's the total wait this bus has
    already endured at earlier stations. Lets us penalise further delay
    for a bus that's already been unlucky.
  - "What is delay_min?" → earliest_arrival - scheduled_arrival.
    Negative = running early. Positive = running late. The solver knows
    the bus *is* late but doesn't use delay as a hard constraint by default
    — you could add one (e.g. deprioritise very-late buses).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

# The physical charging route. Order matters: A->D buses visit in order,
# D->A buses visit in reverse. INTERVIEW: "Add station E" → append here
# and add it to the JSON files. Nothing else changes.
ROUTE = ["A", "B", "C", "D"]

Direction = Literal["A->D", "D->A"]
BusStatus = Literal["charging", "traveling", "waiting", "done"]


@dataclass(frozen=True)
class StationConfig:
    """Configuration for one charging station.

    INTERVIEW: "Station B gets a third charger" → change chargers=3 in
    the JSON. The solver's AddCumulative constraint picks this up
    automatically — no solver code changes needed.
    """
    name: str
    chargers: int  # how many buses can charge simultaneously


@dataclass(frozen=True)
class Weights:
    """Optimization weights. GLOBAL — same for all scenarios.

    individual: penalise per-bus wait time. High = scheduler tries to
                minimise how long any single bus waits at a station.
    operator:   penalise the operator whose fleet waits the most in total.
                High = fairer across operators (no one operator is always
                stuck behind another operator's buses).
    network:    penalise total completion time across all buses. High =
                scheduler tries to get everyone through the system faster.
    delay:      NEW — penalise buses that are already running late
                (earliest_arrival > scheduled_arrival). Bumping this weight
                means late buses get priority, which is a policy choice.

    INTERVIEW: "Add a weight for bus priority" → add a field here, read it
    in solver.py, multiply it by your new term in the objective.
    """
    individual: float   # per-bus wait minimisation
    operator: float     # max-operator-wait minimisation (fairness across operators)
    network: float      # total network completion time
    delay: float        # extra penalty for buses already running late
    intra_operator_priority: float   # Delay penalty within the operator


@dataclass(frozen=True)
class UpcomingStop:
    """One future charging stop for a bus.

    WHAT EACH FIELD DOES FOR THE SOLVER:
      station              → which AddCumulative pool this stop belongs to
      scheduled_arrival_min → informational; used to compute delay_min
      earliest_arrival_min  → hard lower bound: solver cannot start charging
                              before this. This is the physically possible
                              earliest time given current position + speed.
      cumulative_wait_min   → how many minutes this bus has already waited
                              at previous stations on this trip. Used in the
                              objective to give relief to already-suffering buses.
      delay_min             → earliest - scheduled (pre-computed for clarity).
                              Negative means running early; positive means late.
    """
    station: str
    scheduled_arrival_min: int   # when it *should* arrive (original timetable)
    earliest_arrival_min: int    # when it *will* arrive (reality)
    cumulative_wait_min: int     # total wait already accumulated at prior stations
    delay_min: int               # earliest_arrival - scheduled_arrival


@dataclass(frozen=True)
class Bus:
    """One bus in the network at the snapshot moment.

    FIELDS THE SOLVER ACTUALLY USES:
      upcoming            → list of UpcomingStop objects to schedule
      charging_at         → if not None, bus is mid-charge; solver fixes that slot
      charging_started_min→ start of the active charge (negative = started before snapshot)

    FIELDS THE UI USES (not solver inputs):
      status, location_desc, direction, operator, started_at_min
    """
    id: str
    operator: str
    direction: Direction
    status: BusStatus
    location_desc: str           # human-readable: "in transit B->C, ~16 min to arrival"
    started_at_min: int          # minutes from snapshot (negative = started before snapshot)
    upcoming: tuple[UpcomingStop, ...]
    charging_at: str | None = None          # station name if currently charging
    charging_started_min: int | None = None # minutes from snapshot (negative)


@dataclass(frozen=True)
class Scenario:
    """The complete frozen state of the network at one moment.

    NOTE: weights are NOT read from the scenario file. They are a global
    constant defined in the app. The scenario file still carries a 'weights'
    key (for backwards compatibility and human readability) but the solver
    always uses the global GLOBAL_WEIGHTS defined in solver.py.

    INTERVIEW: "What if different scenarios need different weights?" →
    You could re-read weights from the file. Right now the design decision
    is that weights are a system-level policy, not a scenario-level setting.
    That's a defensible choice: the same scheduling policy should apply
    regardless of which snapshot you're looking at.
    """
    name: str
    snapshot_time: str           # "HH:MM" string, for display only
    charge_minutes: int          # how long one charge session takes
    travel_minutes_per_leg: int  # nominal travel time between adjacent stations
    stations: dict[str, StationConfig]
    weights: Weights             # global, not from file (see NOTE above)
    buses: tuple[Bus, ...]
    raw: dict = field(default_factory=dict)  # original JSON for UI display

    def buses_at(self, station: str) -> list[Bus]:
        """Buses that still need to charge at `station`."""
        return [b for b in self.buses if any(u.station == station for u in b.upcoming)]