"""
loader.py — Parse a scenario JSON file into the typed model objects.

WHAT THIS FILE DOES:
  1. Reads the JSON from disk
  2. Converts HH:MM time strings → integer minutes from snapshot
  3. Builds typed Bus / UpcomingStop / Scenario objects
  4. Injects GLOBAL_WEIGHTS (not from file — see model.py for why)

WHY SEPARATE FROM model.py?
  model.py is pure data types — no I/O, no parsing logic.
  loader.py is the I/O boundary. This separation means:
    - You can unit-test model.py with no file system
    - You can swap JSON for YAML or a database by changing only loader.py

KEY FUNCTION: _hhmm_to_min(snapshot, hhmm) → int
  This is the trickiest piece. Scenarios span midnight (a bus starting
  at 20:45 may charge at station D at 02:10 the next morning).
  We can't store dates in HH:MM, so we use a ±12-hour disambiguation rule:
  if the raw difference is > +12h, we assume "actually yesterday";
  if < -12h, we assume "actually tomorrow".

INTERVIEW HOT SPOTS:
  "What if a scenario runs for more than 24 hours?" →
    The ±12h rule breaks. You'd need to store a date or an explicit
    minutes offset in the JSON. For this problem (overnight bus routes),
    ±12h is sufficient.
  "What if a new field is added to the JSON?" →
    Add the parsing line here. The model.py dataclass will also need
    the field. Loader is the only place that touches the JSON keys.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import Bus, Scenario, StationConfig, UpcomingStop, Weights

# ──────────────────────────────────────────────────────
# GLOBAL WEIGHTS — one constant for the whole system.
# These are the optimal values we've tuned.
#
# INTERVIEW: "How did you pick these values?"
#   individual=1.5: strong incentive to reduce per-bus wait. Most important.
#   operator=1.0:   fairness across operators matters but less than raw wait.
#   network=0.5:    total completion time is a tie-breaker, not a priority.
#   delay=0.8:      buses already running late get moderate priority boost.
#
# "How would you change them?" → Edit the numbers below. One place.
# "What happens if individual is very high?" → Solver aggressively minimises
#   the single worst wait, possibly at the cost of total network efficiency.
# "What happens if operator is very high?" → Solver balances across operators
#   even if that increases individual waits.
# ──────────────────────────────────────────────────────
GLOBAL_WEIGHTS = Weights(
    individual=1.5,
    operator=1.0,
    network=0.5,
    delay=0.0,
    intra_operator_priority=0.7,
)


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

    IMPORTANT DECISIONS HERE:
    1. weights are IGNORED from the file — GLOBAL_WEIGHTS is used instead.
    2. cumulative_wait_min is read from the JSON (generator sets it to 0;
       after the solver runs we could write it back, but we don't yet).
    3. delay_min in the JSON is purely informational. We recompute it from
       earliest_arrival - scheduled_arrival to be safe (in case of rounding).
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
            earliest_min = _hhmm_to_min(snapshot, u["earliest_arrival"])
            # delay_min: positive = late, negative = early
            delay_min = earliest_min - sched_min
            upcoming_stops.append(UpcomingStop(
                station=u["station"],
                scheduled_arrival_min=sched_min,
                earliest_arrival_min=earliest_min,
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
        weights=GLOBAL_WEIGHTS,   # ← global constant, not from file
        buses=tuple(buses),
        raw=raw,                  # keep original for UI "Raw JSON" tab
    )


def list_scenarios(folder: str | Path) -> list[Path]:
    """Return all scenario_*.json files sorted by name."""
    return sorted(Path(folder).glob("scenario_*.json"))