"""
CP-SAT scheduler.

The model decides, per bus, *which* charging stations to use and *when* charging
starts at each. Hard rules (range, charger capacity, route order) are encoded as
constraints; the soft objective is a weighted combination of three terms read
from the scenario file.

Adding a new hard rule = one `model.Add(...)` call.
Adding a new soft term = one new variable and one entry in the `Minimize(...)`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ortools.sat.python import cp_model

from .model import Bus, Scenario


WEIGHT_SCALE = 1000   # CP-SAT requires integer coefficients


# ──────────────────────────────────────────────
# Output shapes
# ──────────────────────────────────────────────

@dataclass(frozen=True)
class ChargeEvent:
    """One charging session — appears in both per-bus and per-station views."""
    bus_id: str
    operator: str
    direction: str
    station: str
    arrive_min: int          # when bus reached the station
    start_min: int           # when charging actually started
    end_min: int             # = start_min + charge_minutes
    wait_min: int            # = max(start - arrive, 0)


@dataclass(frozen=True)
class BusTimeline:
    """The full plan for one bus, departure → arrival."""
    bus_id: str
    operator: str
    direction: str
    departure_min: int
    arrival_at_destination_min: int
    total_wait_min: int
    total_charge_min: int          # sum of charge sessions (= 25 × #charges)
    charges: tuple[ChargeEvent, ...]   # in route order


@dataclass
class Schedule:
    """The full solver output."""
    by_bus: dict[str, BusTimeline]
    by_station: dict[str, list[ChargeEvent]]
    status: str
    objective: float
    total_wait_min: int
    total_charge_min: int

    def order_at(self, station: str) -> list[ChargeEvent]:
        """Charging order at one station — by start time."""
        return sorted(self.by_station.get(station, []), key=lambda e: e.start_min)

    def total_wait_by_operator(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for tl in self.by_bus.values():
            out[tl.operator] = out.get(tl.operator, 0) + tl.total_wait_min
        return out


# ──────────────────────────────────────────────
# Per-bus path geometry — pre-computed once before model building
# ──────────────────────────────────────────────

@dataclass
class _BusPath:
    """Distances and travel times along one bus's directional path."""
    bus: Bus
    stations: list[str]                   # inner stations in route order
    dist_from_origin: dict[str, int]      # station name → km from origin
    total_distance_km: int                # full origin→destination distance
    last_station_to_destination_km: int   # km from last station to destination


def _build_paths(scenario: Scenario) -> dict[str, _BusPath]:
    paths: dict[str, _BusPath] = {}
    for bus in scenario.buses:
        nodes = scenario.route.path(bus.direction)
        cum = 0
        dist_from_origin: dict[str, int] = {}
        stations: list[str] = []
        for i, node in enumerate(nodes):
            if i > 0:
                cum += scenario.route.distance_between(nodes[i - 1], node)
            if node in scenario.stations:
                stations.append(node)
                dist_from_origin[node] = cum
        total = cum
        last_to_dest = total - dist_from_origin[stations[-1]] if stations else total
        paths[bus.id] = _BusPath(
            bus=bus,
            stations=stations,
            dist_from_origin=dist_from_origin,
            total_distance_km=total,
            last_station_to_destination_km=last_to_dest,
        )
    return paths


# ──────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────

def solve(scenario: Scenario, time_limit_s: float = 30.0) -> Schedule:
    model = cp_model.CpModel()
    charge = scenario.charge_minutes
    R = scenario.battery_range_km

    paths = _build_paths(scenario)

    # Time horizon: latest possible departure + worst-case full traversal +
    # generous slack for waits in heavy contention.
    earliest = min((b.departure_min for b in scenario.buses), default=0)
    latest_dep = max((b.departure_min for b in scenario.buses), default=0)
    max_total_distance = max((p.total_distance_km for p in paths.values()), default=0)
    nominal_trip = scenario.travel_min(max_total_distance) + len(scenario.stations) * charge
    horizon = latest_dep + nominal_trip + len(scenario.buses) * charge + 240

    # ──────────────────────────────────────────
    # Decision variables
    # ──────────────────────────────────────────
    x: dict[tuple[str, str], cp_model.IntVar] = {}            # presence (Bool)
    start: dict[tuple[str, str], cp_model.IntVar] = {}        # charge start
    end: dict[tuple[str, str], cp_model.IntVar] = {}          # charge end
    interval: dict[tuple[str, str], cp_model.IntervalVar] = {}
    arrive: dict[tuple[str, str], cp_model.IntVar] = {}       # arrival at station
    leave: dict[tuple[str, str], cp_model.IntVar] = {}        # departs station
    wait: dict[tuple[str, str], cp_model.IntVar] = {}         # wait at station
    final_arrival: dict[str, cp_model.IntVar] = {}            # arrival at destination

    for bus in scenario.buses:
        bp = paths[bus.id]
        # Origin "leave time" is just the departure — it's a fixed constant.
        prev_leave: cp_model.IntVar = model.NewConstant(bus.departure_min)
        prev_dist_from_origin = 0

        for st in bp.stations:
            seg_km = bp.dist_from_origin[st] - prev_dist_from_origin
            travel_to_here = scenario.travel_min(seg_km)

            arr = model.NewIntVar(earliest, horizon, f"arr_{bus.id}_{st}")
            model.Add(arr == prev_leave + travel_to_here)

            x_var = model.NewBoolVar(f"use_{bus.id}_{st}")
            s_var = model.NewIntVar(earliest, horizon, f"st_{bus.id}_{st}")
            e_var = model.NewIntVar(earliest, horizon, f"en_{bus.id}_{st}")
            iv = model.NewOptionalIntervalVar(
                s_var, charge, e_var, x_var, f"iv_{bus.id}_{st}"
            )

            # If charging here, can't start before arrival.
            model.Add(s_var >= arr).OnlyEnforceIf(x_var)

            # Wait at this station — 0 if not charging.
            w_var = model.NewIntVar(0, horizon, f"wait_{bus.id}_{st}")
            model.Add(w_var == s_var - arr).OnlyEnforceIf(x_var)
            model.Add(w_var == 0).OnlyEnforceIf(x_var.Not())

            # Leave time: charge end if used, else just pass through.
            lv = model.NewIntVar(earliest, horizon, f"leave_{bus.id}_{st}")
            model.Add(lv == e_var).OnlyEnforceIf(x_var)
            model.Add(lv == arr).OnlyEnforceIf(x_var.Not())

            x[(bus.id, st)] = x_var
            start[(bus.id, st)] = s_var
            end[(bus.id, st)] = e_var
            interval[(bus.id, st)] = iv
            arrive[(bus.id, st)] = arr
            leave[(bus.id, st)] = lv
            wait[(bus.id, st)] = w_var

            prev_leave = lv
            prev_dist_from_origin = bp.dist_from_origin[st]

        # Final leg — last station to destination. Always traversed; no charging.
        last_seg_min = scenario.travel_min(bp.last_station_to_destination_km)
        fa = model.NewIntVar(earliest, horizon, f"final_arr_{bus.id}")
        model.Add(fa == prev_leave + last_seg_min)
        final_arrival[bus.id] = fa

    # ──────────────────────────────────────────
    # Hard rule: battery range
    # ──────────────────────────────────────────
    # Path indices 0..n+1 with 0=origin (always "charged"), n+1=destination.
    # For every (i,j) with i<j and d[j]-d[i] > R, at least one of the stations
    # strictly between (or one of i,j if they're stations) must be USED. We
    # express this as a linear constraint with the endpoints as constants 1.
    for bus in scenario.buses:
        bp = paths[bus.id]
        n = len(bp.stations)
        d = [0] + [bp.dist_from_origin[s] for s in bp.stations] + [bp.total_distance_km]
        # x_path[k] for k in 1..n is the BoolVar; for k=0 and k=n+1 it's a constant 1.
        x_path: list = [None]
        for s in bp.stations:
            x_path.append(x[(bus.id, s)])
        x_path.append(None)

        for i in range(0, n + 2):
            for j in range(i + 1, n + 2):
                if d[j] - d[i] <= R:
                    continue
                # x_i + x_j <= 1 + sum_{i<k<j} x_k    (with endpoints fixed to 1)
                lhs = []
                fixed_lhs = 0
                if i == 0:
                    fixed_lhs += 1
                else:
                    lhs.append(x_path[i])
                if j == n + 1:
                    fixed_lhs += 1
                else:
                    lhs.append(x_path[j])
                rhs = [x_path[k] for k in range(i + 1, j)]   # k always 1..n here
                if lhs or rhs:
                    model.Add(sum(lhs) + fixed_lhs <= 1 + sum(rhs))
                else:
                    # Both endpoints, no stations between (only happens with n=0)
                    # Only feasible if d[j]-d[i] <= R, contradicting the gate above.
                    # Force infeasible explicitly.
                    model.AddBoolAnd([model.NewConstant(0)])

    # ──────────────────────────────────────────
    # Hard rule: charger capacity per station
    # ──────────────────────────────────────────
    for st_name, cfg in scenario.stations.items():
        ivs = [interval[(b.id, st_name)] for b in scenario.buses
               if (b.id, st_name) in interval]
        if not ivs:
            continue
        if cfg.chargers == 1:
            # NoOverlap is the natural primitive for one charger.
            model.AddNoOverlap(ivs)
        else:
            model.AddCumulative(ivs, [1] * len(ivs), cfg.chargers)

    # ──────────────────────────────────────────
    # Per-bus cost: time controllable by the scheduler
    #   = total wait at chargers + total time spent charging
    # Travel time is fixed by physics, so it does not appear here.
    # ──────────────────────────────────────────
    bus_cost: dict[str, cp_model.IntVar] = {}
    bus_total_wait: dict[str, cp_model.IntVar] = {}

    n_stations = len(scenario.stations)
    per_bus_max = horizon + charge * n_stations

    for bus in scenario.buses:
        bp = paths[bus.id]
        if not bp.stations:
            bus_cost[bus.id] = model.NewConstant(0)
            bus_total_wait[bus.id] = model.NewConstant(0)
            continue
        # 25 × number of charges used
        chg_term = model.NewIntVar(0, charge * len(bp.stations), f"chg_{bus.id}")
        model.Add(chg_term == charge * sum(x[(bus.id, s)] for s in bp.stations))
        # Total wait
        w_term = model.NewIntVar(0, horizon, f"twait_{bus.id}")
        model.Add(w_term == sum(wait[(bus.id, s)] for s in bp.stations))
        # Cost = waits + charge time
        cost = model.NewIntVar(0, per_bus_max, f"cost_{bus.id}")
        model.Add(cost == chg_term + w_term)
        bus_cost[bus.id] = cost
        bus_total_wait[bus.id] = w_term

    # ──────────────────────────────────────────
    # Soft objective — three terms, each weighted independently.
    # ──────────────────────────────────────────
    w = scenario.weights

    # Term 1 — INDIVIDUAL: worst single-bus controllable time (minimax).
    # Captures "no single bus should wait too long."
    if bus_cost:
        individual = model.NewIntVar(0, per_bus_max, "individual_max")
        model.AddMaxEquality(individual, list(bus_cost.values()))
    else:
        individual = model.NewConstant(0)

    # Term 2 — OPERATOR: worst operator's total (minimax across operators).
    # Captures "each operator's fleet should run smoothly as a group."
    by_op: dict[str, list[cp_model.IntVar]] = {}
    for bus in scenario.buses:
        by_op.setdefault(bus.operator, []).append(bus_cost[bus.id])
    op_sums: list[cp_model.IntVar] = []
    for op, costs in by_op.items():
        s = model.NewIntVar(0, per_bus_max * len(costs), f"opsum_{op}")
        model.Add(s == sum(costs))
        op_sums.append(s)
    if op_sums:
        operator_max = model.NewIntVar(
            0, per_bus_max * len(scenario.buses), "operator_max"
        )
        model.AddMaxEquality(operator_max, op_sums)
    else:
        operator_max = model.NewConstant(0)

    # Term 3 — OVERALL: sum across all buses (network throughput).
    if bus_cost:
        overall = model.NewIntVar(
            0, per_bus_max * len(scenario.buses), "overall_sum"
        )
        model.Add(overall == sum(bus_cost.values()))
    else:
        overall = model.NewConstant(0)

    wi = int(round(w.individual * WEIGHT_SCALE))
    wo = int(round(w.operator * WEIGHT_SCALE))
    wn = int(round(w.overall * WEIGHT_SCALE))
    model.Minimize(wi * individual + wo * operator_max + wn * overall)

    # ──────────────────────────────────────────
    # Search hint — try smaller start values first. With multiple buses arriving
    # at the same time, this mimics first-come-first-served and prunes symmetric
    # alternatives.
    # ──────────────────────────────────────────
    all_starts = list(start.values())
    if all_starts:
        model.AddDecisionStrategy(
            all_starts, cp_model.CHOOSE_FIRST, cp_model.SELECT_MIN_VALUE
        )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 4
    status_code = solver.Solve(model)
    status_name = solver.StatusName(status_code)

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Schedule(
            by_bus={},
            by_station={s: [] for s in scenario.stations},
            status=status_name,
            objective=float("inf"),
            total_wait_min=0,
            total_charge_min=0,
        )

    # ──────────────────────────────────────────
    # Extract results
    # ──────────────────────────────────────────
    by_bus: dict[str, BusTimeline] = {}
    by_station: dict[str, list[ChargeEvent]] = {s: [] for s in scenario.stations}
    grand_wait = 0
    grand_charge = 0

    for bus in scenario.buses:
        bp = paths[bus.id]
        events: list[ChargeEvent] = []
        bus_wait = 0
        bus_charge = 0
        for st in bp.stations:
            if not solver.Value(x[(bus.id, st)]):
                continue
            arr_v = int(solver.Value(arrive[(bus.id, st)]))
            s_v = int(solver.Value(start[(bus.id, st)]))
            e_v = int(solver.Value(end[(bus.id, st)]))
            w_v = max(s_v - arr_v, 0)
            ev = ChargeEvent(
                bus_id=bus.id,
                operator=bus.operator,
                direction=bus.direction,
                station=st,
                arrive_min=arr_v,
                start_min=s_v,
                end_min=e_v,
                wait_min=w_v,
            )
            events.append(ev)
            by_station[st].append(ev)
            bus_wait += w_v
            bus_charge += charge

        timeline = BusTimeline(
            bus_id=bus.id,
            operator=bus.operator,
            direction=bus.direction,
            departure_min=bus.departure_min,
            arrival_at_destination_min=int(solver.Value(final_arrival[bus.id])),
            total_wait_min=bus_wait,
            total_charge_min=bus_charge,
            charges=tuple(events),
        )
        by_bus[bus.id] = timeline
        grand_wait += bus_wait
        grand_charge += bus_charge

    return Schedule(
        by_bus=by_bus,
        by_station=by_station,
        status=status_name,
        objective=float(solver.ObjectiveValue()),
        total_wait_min=grand_wait,
        total_charge_min=grand_charge,
    )