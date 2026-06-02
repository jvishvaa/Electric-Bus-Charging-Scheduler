"""
Read a scenario JSON file and return a typed `Scenario`.

The loader is the only file that touches JSON. It validates the shape, converts
HH:MM strings to integer minutes, and assembles the typed objects. Everything
downstream (solver, UI) works with the typed objects only.
"""
from __future__ import annotations

import json
from pathlib import Path

from .model import (
    Bus,
    Route,
    Scenario,
    Segment,
    StationConfig,
    Weights,
)


def _hhmm_to_min(reference: str, hhmm: str) -> int:
    """
    Convert an HH:MM string to minutes from `reference` (also HH:MM).

    Result is signed. If the time-of-day is more than 12h ahead of `reference`,
    we treat it as the previous day; symmetric for the other direction. This
    matters when `reference = 19:00` but a bus arrives at "01:30" the next day.
    """
    rh, rm = (int(p) for p in reference.split(":"))
    h, m = (int(p) for p in hhmm.split(":"))
    raw = (h * 60 + m) - (rh * 60 + rm)
    if raw < -12 * 60:
        raw += 24 * 60
    elif raw > 12 * 60:
        raw -= 24 * 60
    return raw


def load_scenario(path: str | Path) -> Scenario:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))

    reference = raw["reference_time"]

    # Route — list of nodes + segments + endpoints. Stored verbatim so a future
    # scenario can introduce a different route shape without code changes.
    route_raw = raw["route"]
    nodes = tuple(route_raw["nodes"])
    segments = tuple(
        Segment(
            from_node=s["from"],
            to_node=s["to"],
            distance_km=int(s["distance_km"]),
        )
        for s in route_raw["segments"]
    )
    endpoints = (route_raw["endpoints"][0], route_raw["endpoints"][1])
    route = Route(nodes=nodes, segments=segments, endpoints=endpoints)

    # Stations — explicit so a scenario can override charger counts per station.
    stations = {
        name: StationConfig(name=name, chargers=int(cfg.get("chargers", 1)))
        for name, cfg in raw["stations"].items()
    }

    # Weights — read as floats so engineers can tune freely.
    w = raw["weights"]
    weights = Weights(
        individual=float(w["individual"]),
        operator=float(w["operator"]),
        overall=float(w["overall"]),
    )

    # Buses — only departures for now. The solver derives everything else.
    buses = tuple(
        Bus(
            id=b["id"],
            operator=b["operator"],
            direction=b["direction"],
            departure_min=_hhmm_to_min(reference, b["departure"]),
            priority_pass_wait_time=bool(b.get("priority_pass_wait_time", False)),
            min_soc_limit=int(b.get("min_soc_limit", 30)),
        )
        for b in raw["buses"]
    )

    return Scenario(
        name=raw["name"],
        reference_time=reference,
        battery_range_km=int(raw["battery_range_km"]),
        charge_minutes=int(raw["charge_minutes"]),
        speed_kmph=int(raw["speed_kmph"]),
        route=route,
        stations=stations,
        weights=weights,
        buses=buses,
        raw=raw,
    )


def list_scenarios(folder: str | Path) -> list[Path]:
    """Return all scenario_*.json files sorted by name."""
    return sorted(Path(folder).glob("scenario_*.json"))
