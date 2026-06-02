"""
Emit the 5 scenario JSON files described in the assessment doc.

Each scenario file is the *complete* input the scheduler reads — route, battery
range, charge time, speed, station charger counts, weights, and the bus list
with departure times. Editing one of these files (or dropping in a new one) is
enough to change the world the scheduler operates in; no Python change needed.
"""
from __future__ import annotations

import json
from pathlib import Path

SCENARIOS_DIR = Path(__file__).parent / "scenarios"

# ──────────────────────────────────────────────
# Defaults shared across the 5 scenarios in the doc.
# Anything you change here applies to every generated file unless the scenario
# overrides it (see scenario 4's weights).
# ──────────────────────────────────────────────
REFERENCE_TIME = "19:00"
BATTERY_RANGE_KM = 240
CHARGE_MINUTES = 25
SPEED_KMPH = 60
DIR_FORWARD = "Bengaluru->Kochi"
DIR_REVERSE = "Kochi->Bengaluru"

ROUTE_NODES = ["Bengaluru", "A", "B", "C", "D", "Kochi"]
ROUTE_SEGMENTS = [
    {"from": "Bengaluru", "to": "A", "distance_km": 100},
    {"from": "A",         "to": "B", "distance_km": 120},
    {"from": "B",         "to": "C", "distance_km": 100},
    {"from": "C",         "to": "D", "distance_km": 120},
    {"from": "D",         "to": "Kochi", "distance_km": 100},
]
ROUTE_ENDPOINTS = ["Bengaluru", "Kochi"]
DEFAULT_STATIONS = {s: {"chargers": 1} for s in ["A", "B", "C", "D"]}
DEFAULT_WEIGHTS = {"individual": 1.0, "operator": 1.0, "overall": 1.0}


def _envelope(name: str, buses: list[dict],
              weights: dict | None = None,
              stations: dict | None = None) -> dict:
    return {
        "name": name,
        "reference_time": REFERENCE_TIME,
        "battery_range_km": BATTERY_RANGE_KM,
        "charge_minutes": CHARGE_MINUTES,
        "speed_kmph": SPEED_KMPH,
        "route": {
            "nodes": ROUTE_NODES,
            "segments": ROUTE_SEGMENTS,
            "endpoints": ROUTE_ENDPOINTS,
        },
        "stations": stations or DEFAULT_STATIONS,
        "weights": weights or DEFAULT_WEIGHTS,
        "buses": buses,
    }


def _bus(bus_id: str, operator: str, direction: str, departure: str, priority_pass_wait_time: bool = False, min_soc_limit: int = 30) -> dict:
    return {
        "id": bus_id,
        "operator": operator,
        "direction": direction,
        "departure": departure,
        "priority_pass_wait_time": priority_pass_wait_time,
        "min_soc_limit": min_soc_limit,
    }


# ──────────────────────────────────────────────
# Scenario 1 — Even spacing (every 15 min, 19:00 onward)
# ──────────────────────────────────────────────
def scenario_1() -> dict:
    bk_ops = ["kpn", "freshbus", "flixbus", "kpn", "freshbus",
              "flixbus", "kpn", "freshbus", "flixbus", "kpn"]
    kb_ops = ["freshbus", "flixbus", "kpn", "freshbus", "flixbus",
              "kpn", "freshbus", "flixbus", "kpn", "freshbus"]
    times = ["19:00", "19:15", "19:30", "19:45", "20:00",
             "20:15", "20:30", "20:45", "21:00", "21:15"]
    buses = []
    for i, (op, t) in enumerate(zip(bk_ops, times), start=1):
        buses.append(_bus(f"bus-BK-{i:02d}", op, DIR_FORWARD, t))
    for i, (op, t) in enumerate(zip(kb_ops, times), start=1):
        buses.append(_bus(f"bus-KB-{i:02d}", op, DIR_REVERSE, t))
    return _envelope("Scenario 1 - Even spacing", buses)


# ──────────────────────────────────────────────
# Scenario 2 — Bunched start (8-min cluster, then spaces out)
# ──────────────────────────────────────────────
def scenario_2() -> dict:
    bk_ops = ["kpn", "freshbus", "flixbus", "kpn", "freshbus",
              "flixbus", "kpn", "freshbus", "flixbus", "kpn"]
    bk_times = ["19:00", "19:08", "19:16", "19:24", "19:32",
                "19:40", "19:48", "20:03", "20:18", "20:33"]
    kb_ops = ["freshbus", "flixbus", "kpn", "freshbus", "flixbus",
              "kpn", "freshbus", "flixbus", "kpn", "freshbus"]
    kb_times = ["19:00", "19:08", "19:16", "19:24", "19:32",
                "19:40", "19:48", "20:03", "20:18", "20:33"]
    buses = []
    for i, (op, t) in enumerate(zip(bk_ops, bk_times), start=1):
        buses.append(_bus(f"bus-BK-{i:02d}", op, DIR_FORWARD, t))
    for i, (op, t) in enumerate(zip(kb_ops, kb_times), start=1):
        buses.append(_bus(f"bus-KB-{i:02d}", op, DIR_REVERSE, t))
    return _envelope("Scenario 2 - Bunched start", buses)


# ──────────────────────────────────────────────
# Scenario 3 — Asymmetric load (10 BK, only 4 KB)
# ──────────────────────────────────────────────
def scenario_3() -> dict:
    bk_ops = ["kpn", "freshbus", "flixbus", "kpn", "freshbus",
              "flixbus", "kpn", "freshbus", "flixbus", "kpn"]
    bk_times = ["19:00", "19:15", "19:30", "19:45", "20:00",
                "20:15", "20:30", "20:45", "21:00", "21:15"]
    kb_ops = ["freshbus", "flixbus", "kpn", "freshbus"]
    kb_times = ["19:00", "19:35", "20:10", "20:45"]
    buses = []
    for i, (op, t) in enumerate(zip(bk_ops, bk_times), start=1):
        buses.append(_bus(f"bus-BK-{i:02d}", op, DIR_FORWARD, t))
    for i, (op, t) in enumerate(zip(kb_ops, kb_times), start=1):
        buses.append(_bus(f"bus-KB-{i:02d}", op, DIR_REVERSE, t))
    return _envelope("Scenario 3 - Asymmetric load", buses)


# ──────────────────────────────────────────────
# Scenario 4 — Operator-heavy (KPN dominates BK; operator weight = 2.0)
# ──────────────────────────────────────────────
def scenario_4() -> dict:
    bk_ops = ["kpn", "kpn", "kpn", "kpn", "kpn",
              "kpn", "kpn", "kpn", "freshbus", "flixbus"]
    bk_times = ["19:00", "19:15", "19:30", "19:45", "20:00",
                "20:15", "20:30", "20:45", "21:00", "21:15"]
    kb_ops = ["freshbus", "flixbus", "kpn", "freshbus", "flixbus",
              "kpn", "freshbus", "flixbus", "kpn", "freshbus"]
    kb_times = ["19:00", "19:15", "19:30", "19:45", "20:00",
                "20:15", "20:30", "20:45", "21:00", "21:15"]
    buses = []
    for i, (op, t) in enumerate(zip(bk_ops, bk_times), start=1):
        buses.append(_bus(f"bus-BK-{i:02d}", op, DIR_FORWARD, t))
    for i, (op, t) in enumerate(zip(kb_ops, kb_times), start=1):
        buses.append(_bus(f"bus-KB-{i:02d}", op, DIR_REVERSE, t))
    weights = {"individual": 1.0, "operator": 2.0, "overall": 1.0}
    return _envelope("Scenario 4 - Operator-heavy", buses, weights=weights)


# ──────────────────────────────────────────────
# Scenario 5 — Worst-case convergence (every 8 min from both ends)
# ──────────────────────────────────────────────
def scenario_5() -> dict:
    bk_ops = ["kpn", "freshbus", "flixbus", "kpn", "freshbus",
              "flixbus", "kpn", "freshbus", "flixbus", "kpn"]
    bk_times = ["19:00", "19:08", "19:16", "19:24", "19:32",
                "19:40", "19:48", "19:56", "20:04", "20:12"]
    kb_ops = ["freshbus", "flixbus", "kpn", "freshbus", "flixbus",
              "kpn", "freshbus", "flixbus", "kpn", "freshbus"]
    kb_times = ["19:00", "19:08", "19:16", "19:24", "19:32",
                "19:40", "19:48", "19:56", "20:04", "20:12"]
    buses = []
    for i, (op, t) in enumerate(zip(bk_ops, bk_times), start=1):
        buses.append(_bus(f"bus-BK-{i:02d}", op, DIR_FORWARD, t))
    for i, (op, t) in enumerate(zip(kb_ops, kb_times), start=1):
        buses.append(_bus(f"bus-KB-{i:02d}", op, DIR_REVERSE, t))
    return _envelope("Scenario 5 - Worst-case convergence", buses)


def main() -> None:
    SCENARIOS_DIR.mkdir(exist_ok=True)
    builders = {
        "scenario_1_even_spacing.json":      scenario_1,
        "scenario_2_bunched_start.json":     scenario_2,
        "scenario_3_asymmetric_load.json":   scenario_3,
        "scenario_4_operator_heavy.json":    scenario_4,
        "scenario_5_worst_case.json":        scenario_5,
    }
    for filename, fn in builders.items():
        data = fn()
        path = SCENARIOS_DIR / filename
        path.write_text(json.dumps(data, indent=2))
        n = len(data["buses"])
        bk = sum(1 for b in data["buses"] if b["direction"] == DIR_FORWARD)
        print(f"wrote {filename}: {n} buses ({bk} BK, {n - bk} KB)")


if __name__ == "__main__":
    main()
