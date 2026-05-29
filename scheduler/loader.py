from __future__ import annotations

import json
from pathlib import Path

from .model import Bus, Scenario, StationConfig, UpcomingStop, Weights



def _hhmm_to_min(snapshot: str, hhmm: str) -> int:
    """
    Convert an HH:MM time string to minutes from the snapshot time.
    Returns a signed integer: negative = before snapshot, positive = after.

    Example:
      snapshot = "20:45", hhmm = "21:00" → +15
      snapshot = "20:45", hhmm = "20:30" → -15
      snapshot = "20:45", hhmm = "02:10" → +325 (next day, +12h rule)
    """
    sh, sm = (int(p) for p in snapshot.split(":"))
    h, m = (int(p) for p in hhmm.split(":"))
    raw = (h * 60 + m) - (sh * 60 + sm)
    # Midnight disambiguation
    if raw > 12 * 60:
        raw -= 24 * 60   # actually the previous day
    elif raw < -12 * 60:
        raw += 24 * 60   # actually the next day
    return raw


def load_scenario(path: str | Path) -> Scenario:
    """
    Read a JSON file and return a fully-typed Scenario.
    
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    snapshot = raw["snapshot_time"]

    # Parse stations
    stations = {
        name: StationConfig(name=name, chargers=int(cfg["chargers"]))
        for name, cfg in raw["stations"].items()
    }

    # Parse buses
    buses: list[Bus] = []
    for b in raw["buses"]:
        # Parse upcoming stops
        upcoming_stops: list[UpcomingStop] = []
        for u in b.get("upcoming", []):
            sched_min = _hhmm_to_min(snapshot, u["scheduled_arrival"])
            actual_min = _hhmm_to_min(snapshot, u["actual_arrival"])
            # delay_min: positive = late, negative = early
            delay_min = actual_min - sched_min
            upcoming_stops.append(UpcomingStop(
                station=u["station"],
                scheduled_arrival_min=sched_min,
                actual_arrival_min=actual_min,
                cumulative_wait_min=int(u.get("cumulative_wait_min", 0)),
                delay_min=delay_min,
            ))

        # Parse optional charging fields
        charging_at = b.get("charging_at")
        charging_started_raw = b.get("charging_started_at")
        charging_started_min = (
            _hhmm_to_min(snapshot, charging_started_raw)
            if charging_started_raw else None
        )

        buses.append(Bus(
            id=b["id"],
            operator=b["operator"],
            direction=b["direction"],
            status=b["status"],
            location_desc=b.get("location_desc", ""),
            started_at_min=_hhmm_to_min(snapshot, b["started_at"]),
            upcoming=tuple(upcoming_stops),
            charging_at=charging_at,
            charging_started_min=charging_started_min,
        ))

    return Scenario(
        name=raw["name"],
        snapshot_time=snapshot,
        charge_minutes=int(raw.get("charge_minutes", 15)),
        travel_minutes_per_leg=int(raw.get("travel_minutes_per_leg", 150)),
        stations=stations,
        weights=Weights(
            individual=float(raw["weights"]["individual"]),
            operator=float(raw["weights"]["operator"]),
            network=float(raw["weights"]["network"]),
            intra_operator_priority=float(raw["weights"]["intra_operator_priority"])
        ),
        buses=tuple(buses),
        raw=raw,                  # keep original for UI "Raw JSON" tab
    )


def list_scenarios(folder: str | Path) -> list[Path]:
    """Return all scenario_*.json files sorted by name."""
    return sorted(Path(folder).glob("scenario_*.json"))