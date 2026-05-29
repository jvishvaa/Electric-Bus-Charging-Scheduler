from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal

ROUTE = ["A", "B", "C", "D"]

Direction = Literal["A->D", "D->A"]
BusStatus = Literal["charging", "traveling", "waiting", "done"]


@dataclass(frozen=True)
class StationConfig:
    """
      Configuration for one charging station.
    """
    name: str
    chargers: int  # how many buses can charge simultaneously


@dataclass(frozen=True)
class Weights:

    individual: float   # per-bus wait minimisation
    operator: float     # max-operator-wait minimisation (fairness across operators)
    network: float      # total network completion time
    intra_operator_priority: float   # Delay penalty within the operator


@dataclass(frozen=True)
class UpcomingStop:

    station: str
    scheduled_arrival_min: int   # when it *should* arrive (original timetable)
    actual_arrival_min: int    # when it *will* arrive (reality)
    cumulative_wait_min: int     # total wait already accumulated at prior stations
    delay_min: int               # actual_arrival - scheduled_arrival


@dataclass(frozen=True)
class Bus:
    """
    One bus in the network at the snapshot moment.
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
    """
    The complete frozen state of the network at one moment.
    """
    name: str
    snapshot_time: str           # "HH:MM" string, for display only
    charge_minutes: int          # how long one charge session takes
    travel_minutes_per_leg: int  # nominal travel time between adjacent stations
    stations: dict[str, StationConfig]
    weights: Weights             # from the file
    buses: tuple[Bus, ...]
    raw: dict = field(default_factory=dict)  # original JSON for UI display

    def buses_at(self, station: str) -> list[Bus]:
        """Buses that still need to charge at `station`."""
        return [b for b in self.buses if any(u.station == station for u in b.upcoming)]