"""
generate_scenarios.py — Creates the 5 scenario JSON files in scenarios/.

Run once:  python generate_scenarios.py

WHY A GENERATOR SCRIPT INSTEAD OF HAND-WRITTEN JSON?
  Adding a 6th scenario = copy one function and tweak the perturbation.
  The generator handles all the timing arithmetic correctly.
  Hand-writing 40-bus timing data would be error-prone.

THE KEY TIMING ARITHMETIC (understand this for the interview):
  Each bus starts at some offset from snapshot (negative = started before snapshot).
  It charges at each station for CHARGE minutes, then travels TRAVEL minutes.
  So if started_offset = -360 and direction = A->D:
    Station A: arrives at -360, charges until -345
    Station B: arrives at -360 + CHARGE + TRAVEL = -360 + 15 + 150 = -195
    Station C: arrives at -195 + 165 = -30
    Station D: arrives at -30 + 165 = 135 (135 min after snapshot = 23:00)

PERTURBATIONS:
  Each scenario adds extra minutes on certain legs to simulate real-world
  conditions (traffic, early drivers, etc.). The perturbation only affects
  actual_arrival — scheduled_arrival always uses nominal timing.

CUMULATIVE WAIT:
  We don't know actual wait times during generation (that's the solver's job).
  So cumulative_wait_min = 0 for all stops at generation time.
  The solver fills in actual wait after solving. We include it in the JSON
  as 0 because the loader always expects the field.

INTERVIEW: "Add scenario 6 — station C charger is down" →
  Copy scenario_1_baseline(), change chargers_per_station for C to 1 in
  the envelope, give it a new name. Done.
"""

from __future__ import annotations

import json
import random
from pathlib import Path

ROUTE = ["A", "B", "C", "D"]
SNAPSHOT = "20:45"
SNAPSHOT_MIN = 20 * 60 + 45   # 1245 minutes since midnight
TRAVEL = 150                   # nominal minutes per leg
CHARGE = 15                    # minutes per charge session
OPERATORS = ["kpn", "freshbus", "flixbus"]
SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ──────────────────────────────────────────────
# Helper: integer minutes → "HH:MM" string
# ──────────────────────────────────────────────
def _hhmm(minutes_from_midnight: int) -> str:
    """Convert absolute minutes (since midnight) to HH:MM string.
    Wraps around midnight cleanly.
    """
    minutes_from_midnight %= 24 * 60
    return f"{minutes_from_midnight // 60:02d}:{minutes_from_midnight % 60:02d}"


def _from_snapshot(min_offset: int) -> str:
    """Convert minutes-from-snapshot to HH:MM string."""
    return _hhmm(SNAPSHOT_MIN + min_offset)


# ──────────────────────────────────────────────
# Route helpers
# ──────────────────────────────────────────────
def _route_for(direction: str) -> list[str]:
    """A->D buses visit A,B,C,D. D->A buses visit D,C,B,A."""
    return ROUTE if direction == "A->D" else list(reversed(ROUTE))


# ──────────────────────────────────────────────
# Single-bus planner
# ──────────────────────────────────────────────
def _plan_bus(bus_id: str, operator: str, direction: str,
              started_offset: int,
              perturbation_fn=None) -> dict:
    """
    Compute the complete timeline for one bus and return it as a JSON-ready dict.

    perturbation_fn(from_station, to_station) → extra minutes added to REAL
    travel time on that leg. Scheduled time is always nominal (no perturbation).

    INTERVIEW: "What does actual_arrival represent vs scheduled_arrival?"
      scheduled = what the original timetable promised.
      actual    = what physics allows given current position and speed.
      If actual > scheduled, the bus is LATE.
      If actual < scheduled, the bus is EARLY.
      The solver uses actual as the hard lower bound for when charging can start.
    """
    perturb = perturbation_fn or (lambda a, b: 0)
    route = _route_for(direction)

    # Walk the route accumulating times
    # sched[station] = minutes from snapshot when bus should arrive (nominal)
    # real[station]  = minutes from snapshot when bus will actually arrive
    sched: dict[str, int] = {}
    real: dict[str, int] = {}

    t_sched = started_offset
    t_real = started_offset
    for i, station in enumerate(route):
        sched[station] = t_sched
        real[station] = t_real
        if i < len(route) - 1:
            extra = perturb(station, route[i + 1])
            t_sched += CHARGE + TRAVEL          # nominal
            t_real += CHARGE + TRAVEL + extra   # reality

    # ── Determine bus's current state at snapshot (offset = 0) ──
    visited: list[str] = []
    current_station: str | None = None
    status = "traveling"
    in_from = in_to = None
    in_remaining: int | None = None
    charging_started_offset: int | None = None

    for i, station in enumerate(route):
        arrive = real[station]
        depart = arrive + CHARGE  # when charging finishes

        if arrive > 0:
            # Bus hasn't reached this station yet at snapshot time.
            # It's in transit on the previous leg.
            prev = route[i - 1]
            in_from, in_to = prev, station
            in_remaining = arrive   # minutes until arrival from snapshot
            status = "traveling"
            current_station = None
            break

        if arrive <= 0 < depart:
            # Bus is currently charging here (charging window straddles snapshot)
            current_station = station
            status = "charging"
            charging_started_offset = arrive
            break

        if depart <= 0:
            # Bus has already finished here
            visited.append(station)
            if i == len(route) - 1:
                current_station = station
                status = "done"

    # ── Build the upcoming list ──
    # "upcoming" = stations not yet completed, excluding the live active charge
    upcoming = []
    for station in route:
        if station in visited:
            continue
        if status == "charging" and station == current_station:
            # This is the currently-active charge. We DON'T add it to
            # upcoming because the solver models it as a fixed interval.
            # The UI shows it separately via charging_at / charging_started_at.
            continue
        upcoming.append({
            "station": station,
            "scheduled_arrival": _from_snapshot(sched[station]),
            "actual_arrival": _from_snapshot(real[station]),
            "cumulative_wait_min": 0,  # solver fills this in after solving
            "delay_min": real[station] - sched[station],
        })

    # ── Build location_desc for UI ──
    if status == "charging":
        location_desc = f"charging at {current_station}"
    elif status == "traveling":
        location_desc = (
            f"in transit {in_from}->{in_to}, ~{in_remaining} min to arrival"
        )
    elif status == "done":
        location_desc = f"completed route at {current_station}"
    else:
        location_desc = "waiting"

    obj: dict = {
        "id": bus_id,
        "operator": operator,
        "direction": direction,
        "started_at": _from_snapshot(started_offset),
        "status": status,
        "location_desc": location_desc,
        "upcoming": upcoming,
    }
    if status == "charging":
        obj["charging_at"] = current_station
        obj["charging_started_at"] = _from_snapshot(charging_started_offset)

    return obj


# ──────────────────────────────────────────────
# Fleet builder
# ──────────────────────────────────────────────
def _make_fleet(label: str, started_offsets: list[int], directions: list[str],
                perturbation_for, chargers_per_station: int = 2) -> list[dict]:
    """
    Build all 40 buses. Applies a snapshot-validity guard: if more buses
    than chargers are simultaneously charging at one station at snapshot time,
    demote the excess to 'waiting'.

    WHY THE GUARD EXISTS:
      The generator may produce 3 buses all mid-charge at station B at 20:45,
      but station B only has 2 chargers. This is physically impossible.
      We sort by who started charging earliest (they get the real charger),
      and demote the rest to waiting status. Their upcoming list already
      includes the station so the solver re-schedules them.

    INTERVIEW: "What if you didn't have this guard?"
      The solver would receive a scenario where 3 buses claim a charger that
      holds 2. The solver would still produce a valid answer because AddCumulative
      enforces capacity — but the *scenario data itself* would be physically
      inconsistent (a lie about the current world state).
    """
    assert len(started_offsets) == 40 and len(directions) == 40

    buses = []
    for i in range(40):
        operator = OPERATORS[i % 3]  # kpn, freshbus, flixbus, kpn, freshbus, ...
        bus = _plan_bus(
            bus_id=f"{label}-{i+1:02d}",
            operator=operator,
            direction=directions[i],
            started_offset=started_offsets[i],
            perturbation_fn=lambda a, b, _i=i: perturbation_for(_i, a, b),
        )
        # If bus is "done" at snapshot, push its start forward by 60 min so it
        # still has upcoming stops to schedule. Keeps fleet sizes at 40 active buses.
        if bus["status"] == "done":
            bus = _plan_bus(
                bus_id=f"{label}-{i+1:02d}",
                operator=operator,
                direction=directions[i],
                started_offset=started_offsets[i] + 60,
                perturbation_fn=lambda a, b, _i=i: perturbation_for(_i, a, b),
            )
        buses.append(bus)

    # Snapshot-validity guard: cap active charges at charger limit
    by_station: dict[str, list] = {}
    for b in buses:
        if b["status"] == "charging":
            by_station.setdefault(b["charging_at"], []).append(b)

    for station, charging_buses in by_station.items():
        if len(charging_buses) <= chargers_per_station:
            continue
        # Sort by charging_started_at — earliest starters keep the charger
        charging_buses.sort(key=lambda b: b["charging_started_at"])
        for b in charging_buses[chargers_per_station:]:
            # Demote to waiting
            b["status"] = "waiting"
            b["location_desc"] = f"waiting at {station}"
            del b["charging_at"]
            del b["charging_started_at"]
            # Add station back to upcoming (it wasn't there since it was "active")
            # Reconstruct the upcoming entry at offset 0 (bus is already here)
            b["upcoming"].insert(0, {
                "station": station,
                "scheduled_arrival": _from_snapshot(0),  # approximate
                "actual_arrival": _from_snapshot(0),
                "cumulative_wait_min": 0,
                "delay_min": 0,
            })

    return buses


# ──────────────────────────────────────────────
# Stagger helpers
# ──────────────────────────────────────────────
def _staggered_starts(n: int, seed: int, *, base_min: int = -360,
                      step: int = 12, jitter: int = 6) -> list[int]:
    """
    Stagger bus departure times across a ~8-hour window.
    base_min = first bus left this many minutes before snapshot.
    Without staggering, all buses hit the same station at the same time
    → degenerate scheduling problem.
    """
    rng = random.Random(seed)
    return [base_min + i * step + rng.randint(-jitter, jitter) for i in range(n)]


def _alternating_directions() -> list[str]:
    """20 A->D, 20 D->A, interleaved so each operator has both directions."""
    return ["A->D" if i % 2 == 0 else "D->A" for i in range(40)]


# ──────────────────────────────────────────────
# Scenario envelope
# ──────────────────────────────────────────────
def _envelope(name: str, buses: list[dict],
              chargers: dict[str, int] | None = None) -> dict:
    """
    Wrap buses in the full scenario JSON structure.

    Weights are defined directly here. To change them for ALL scenarios,
    edit the dict below and re-run generate_scenarios.py. To change for
    ONE scenario only, edit that scenario's JSON file directly after generation.

    INTERVIEW: "How do you change weights for a specific scenario?"
    → Edit the weights block in that scenario's JSON file directly.
      The loader reads weights from the JSON so no code change is needed.
    "How do you change weights globally?"
    → Edit the weights dict in _envelope() below and re-run the generator.
    """
    chargers = chargers or {s: 2 for s in ["A", "B", "C", "D"]}
    return {
        "name": name,
        "snapshot_time": SNAPSHOT,
        "charge_minutes": CHARGE,
        "travel_minutes_per_leg": TRAVEL,
        "weights": {
            "individual": 1.5,
            "operator": 1.0,
            "network": 0.5,
            "intra_operator_priority": 0.8,
        },
        "stations": {s: {"chargers": c} for s, c in chargers.items()},
        "buses": buses,
    }


# ──────────────────────────────────────────────
# The 5 scenario builders
# ──────────────────────────────────────────────

def scenario_1_baseline() -> dict:
    """
    SCENARIO 1 — Everything on schedule.
    No perturbation. Real == scheduled for every bus.
    Expected result: smooth, predictable order. Buses charge in roughly
    first-come-first-served order since no one is late or early.
    """
    starts = _staggered_starts(40, seed=1)
    dirs = _alternating_directions()
    buses = _make_fleet("s1", starts, dirs, lambda i, a, b: 0)
    return _envelope("Scenario 1 - Everything on schedule", buses)


def scenario_2_traffic_block() -> dict:
    """
    SCENARIO 2 — Traffic block between B and C, gradually clearing after 22:00.

    TRAFFIC MODEL:
      Peak delay   : +30 min at snapshot time (20:45) with ±20% random noise
      Decay window : 20:45 → 22:00 (75 min). Delay drops linearly to 0.
      After 22:00  : only small residual noise (±3 min) — traffic has cleared.

    WHY GRADUAL?
      Uniform +30 min for every bus means they all spread out equally →
      zero contention (the previous bug). Gradual clearing means early buses
      (crossing B↔C near 20:45) get hit hard, while later buses cross freely.
      This creates a wave of delayed buses that then catches up with the
      on-time buses ahead → real contention at B and C.

    HOW IT WORKS:
      perturb() receives the bus's SCHEDULED departure from the B↔C origin
      station (pre-computed as sched_bc_offsets[i]).
      decay_factor = max(0, 1 - sched_offset / CLEAR_WINDOW)
        = 1.0 at snapshot, 0.0 at 22:00, stays 0 after.
      actual_delay = BASE_DELAY * decay_factor * random(0.8, 1.2)

    INTERVIEW: "Why does perturb need the scheduled time?"
      The perturbation must depend on WHEN the bus crosses the block, not
      just which bus it is. Two buses on the same leg but at different times
      experience different congestion levels. We pre-compute each bus's
      scheduled B↔C crossing time and pass it through the closure.
    """
    BASE_DELAY   = 30    # minutes delay at peak
    CLEAR_WINDOW = 75    # minutes after snapshot when traffic fully clears (22:00)
    NOISE        = 0.20  # ±20% randomness on delay
    RESIDUAL     = 3     # small noise (min) after traffic clears

    rng = random.Random(2)
    starts = _staggered_starts(40, seed=2)
    dirs   = _alternating_directions()

    # Pre-compute each bus's scheduled arrival at the START of the B↔C leg.
    # A->D: departs B (= arrives B + CHARGE). D->A: departs C (= arrives C + CHARGE).
    # We use the nominal (unperturbed) schedule: start + leg_offsets.
    sched_bc_offsets = []
    for i in range(40):
        direction = dirs[i]
        route = ROUTE if direction == "A->D" else list(reversed(ROUTE))
        # Walk route to find when bus DEPARTS the B↔C origin station
        t = starts[i]
        bc_depart = None
        for idx, station in enumerate(route):
            if idx < len(route) - 1:
                next_st = route[idx + 1]
                if {station, next_st} == {"B", "C"}:
                    bc_depart = t + CHARGE   # departs after charging here
                    break
                t += CHARGE + TRAVEL
        sched_bc_offsets.append(bc_depart if bc_depart is not None else 0)

    def perturb(i, a, b):
        if {a, b} != {"B", "C"}:
            return 0
        sched_offset = sched_bc_offsets[i]  # minutes from snapshot
        # Linear decay: 1.0 at snapshot (offset=0), 0.0 at CLEAR_WINDOW (offset=75).
        # Clamp to [0, 1]: buses already past B<->C (negative offset) get full
        # peak delay (they were in the thick of it). Buses after clear window get 0.
        decay = max(0.0, min(1.0, 1.0 - sched_offset / CLEAR_WINDOW))
        if decay <= 0:
            # Traffic cleared — just small residual noise
            return rng.randint(-RESIDUAL, RESIDUAL)
        # Peak-to-zero linear decay with ±20% noise
        noise_factor = 1.0 + rng.uniform(-NOISE, NOISE)
        return int(round(BASE_DELAY * decay * noise_factor))

    buses = _make_fleet("s2", starts, dirs, perturb)
    return _envelope("Scenario 2 - Traffic block between B and C (gradual clearing)", buses)


def scenario_3_late_starters() -> dict:
    """
    SCENARIO 3 — About 1/3 of buses started 25–40 min late.
    Late buses now collide with on-time buses at stations.
    Operator weight tuned up: we care more about fairness across operators
    because clustering tends to hit one operator's buses harder.

    INTERVIEW: "How does the scheduler handle this?"
    → Late buses arrive at the same time as the bus behind them.
    Two buses hit a 2-charger station simultaneously → one waits.
    The solver picks which one waits based on the weighted objective.
    With individual weight high, it tries to minimise who waits longest.
    """
    rng = random.Random(3)
    starts = _staggered_starts(40, seed=3)
    late_idx = set(rng.sample(range(40), 14))
    # Only shift starts for late buses — this delays all their arrivals
    starts = [s + (rng.randint(25, 40) if i in late_idx else 0)
              for i, s in enumerate(starts)]
    dirs = _alternating_directions()
    buses = _make_fleet("s3", starts, dirs, lambda i, a, b: 0)
    return _envelope("Scenario 3 - Late starters now clashing", buses)


def scenario_4_early_arrivals() -> dict:
    """
    SCENARIO 4 — 10 buses running 10–15 min ahead per leg.
    These buses arrive before the buses that were supposed to charge first.
    Expected challenge: does the scheduler jump early buses ahead of on-time
    buses, or respect original order?
    Answer: depends on weights. With high individual weight, early buses
    get to charge early since they arrived first. With high delay weight,
    late buses get priority even over early-arriving ones.

    INTERVIEW: "Should early buses get to charge early?"
    → Policy question. With current weights (delay=0.5) the solver slightly
    favours on-time / late buses over early ones, but not strongly.
    """
    rng = random.Random(4)
    starts = _staggered_starts(40, seed=4)
    early_idx = set(rng.sample(range(40), 10))

    def perturb(i, a, b):
        if i in early_idx:
            return -rng.randint(10, 15)  # negative = faster travel
        return 0

    dirs = _alternating_directions()
    buses = _make_fleet("s4", starts, dirs, perturb)
    return _envelope("Scenario 4 - Early arrivals", buses)


def scenario_5_mixed_reality() -> dict:
    """
    SCENARIO 5 — A realistic messy evening: some on time, some late,
    some early, some clustered. This is what the network looks like every day.
    Tests general robustness — no single dominant pattern.
    """
    rng = random.Random(5)
    starts = _staggered_starts(40, seed=5, jitter=10)
    late_idx = set(rng.sample(range(40), 10))
    early_idx = set(rng.sample([i for i in range(40) if i not in late_idx], 8))

    def perturb(i, a, b):
        if i in late_idx:
            return rng.randint(5, 12)
        if i in early_idx:
            return -rng.randint(3, 8)
        return 0

    dirs = _alternating_directions()
    buses = _make_fleet("s5", starts, dirs, perturb)
    return _envelope("Scenario 5 - Mixed reality", buses)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    SCENARIOS_DIR.mkdir(exist_ok=True)
    builders = {
        "scenario_1_baseline.json":       scenario_1_baseline,
        "scenario_2_traffic_block.json":  scenario_2_traffic_block,
        "scenario_3_late_starters.json":  scenario_3_late_starters,
        "scenario_4_early_arrivals.json": scenario_4_early_arrivals,
        "scenario_5_mixed_reality.json":  scenario_5_mixed_reality,
    }
    for filename, fn in builders.items():
        data = fn()
        path = SCENARIOS_DIR / filename
        path.write_text(json.dumps(data, indent=2))
        total = len(data["buses"])
        ad = sum(1 for b in data["buses"] if b["direction"] == "A->D")
        statuses = {}
        for b in data["buses"]:
            statuses[b["status"]] = statuses.get(b["status"], 0) + 1
        print(f"✅ {filename}: {total} buses ({ad} A->D, {total-ad} D->A) | {statuses}")


if __name__ == "__main__":
    main()