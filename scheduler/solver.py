from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .model import ROUTE, Bus, Scenario

WEIGHT_SCALE = 1000   # CP-SAT requires integer coefficients; scale floats by this


@dataclass
class ChargingSlot:
    """
    One scheduled charging session for one bus at one station.
    """
    bus_id: str
    operator: str
    direction: str
    station: str
    start_min: int
    end_min: int
    actual_arrival_min: int
    scheduled_arrival_min: int
    cumulative_wait_min: int   # wait accumulated at previous stations
    delay_min: int             # actual - scheduled for this stop

    @property
    def curr_wait_min(self) -> int:
        """How long did this bus wait at this station before charging started?"""
        return max(self.start_min - self.actual_arrival_min, 0)

    @property
    def total_wait_min(self) -> int:
        """cumulative from prior stations + current station wait."""
        return self.cumulative_wait_min + self.curr_wait_min


@dataclass
class Schedule:
    
    """Full solver output."""
    
    by_station: dict[str, list[ChargingSlot]]
    total_wait_min: int          # sum of curr_wait_min across all slots
    objective: float
    status: str

    def order_at(self, station: str) -> list[ChargingSlot]:
        """Slots at a station sorted by start time (charging order)."""
        return sorted(self.by_station.get(station, []), key=lambda s: s.start_min)

    def total_wait_by_operator(self) -> dict[str, int]:
        """Total wait per operator across all stations. For UI display."""
        result: dict[str, int] = {}
        for slots in self.by_station.values():
            for slot in slots:
                result[slot.operator] = result.get(slot.operator, 0) + slot.curr_wait_min
        return result


def solve(scenario: Scenario, time_limit_s: float = 20.0) -> Schedule:
    """
    Build the CP-SAT model and solve it.
    """
    
    model = cp_model.CpModel()
    charge = scenario.charge_minutes
    travel = scenario.travel_minutes_per_leg

    # actual: the minimum time in our system (could be negative if buses
    # started charging before snapshot)
    actual_times = [0]
    for bus in scenario.buses:
        if bus.charging_started_min is not None:
            actual_times.append(bus.charging_started_min)
        for stop in bus.upcoming:
            actual_times.append(stop.actual_arrival_min)
    actual = min(actual_times)

    latest_times = [u.actual_arrival_min for b in scenario.buses for u in b.upcoming]
    latest_arrival = max(latest_times) if latest_times else 0

    # Upper bound: worst case = every bus queues behind every other at same station
    horizon = latest_arrival + charge * (len(scenario.buses) + 2) + 120

    # intervals[(bus_id, station)] = (start_var, end_var, interval_var)
    intervals: dict[tuple[str, str], tuple] = {}

    for bus in scenario.buses:
        prev_end = None   # tracks the end of the previous stop for precedence

        # If bus is currently charging: model as FIXED interval.
        # WHY? The charger is already physically occupied. The solver must
        # respect this — it can't reschedule a charge already in progress.
        if bus.charging_at and bus.charging_started_min is not None:
            s = bus.charging_started_min
            e = s + charge
            start = model.NewConstant(s)
            end = model.NewConstant(e)
            iv = model.NewIntervalVar(
                start, charge, end,
                f"iv_live_{bus.id}_{bus.charging_at}"
            )
            intervals[(bus.id, bus.charging_at)] = (start, end, iv)
            prev_end = end   # this fixed end feeds into the precedence chain

        # For each upcoming stop, create decision variables
        for stop in bus.upcoming:
            key = (bus.id, stop.station)
            if key in intervals:
                # Already modelled as a fixed interval (live charge). Skip.
                _, end_var, _ = intervals[key]
                prev_end = end_var
                continue

            start = model.NewIntVar(actual, horizon, f"start_{bus.id}_{stop.station}")
            end = model.NewIntVar(actual, horizon, f"end_{bus.id}_{stop.station}")
            iv = model.NewIntervalVar(
                start, charge, end,
                f"iv_{bus.id}_{stop.station}"
            )
            intervals[key] = (start, end, iv)

            # Bus cannot start charging before it physically arrives.
            # This is the fundamental lower bound from the real world.
            model.Add(start >= stop.actual_arrival_min)

            # Bus must finish at previous station AND travel before arriving here.

            # Combined with H3a (actual arrival), CP-SAT effectively takes
            # the max of both: start ≥ max(actual_arrival, prev_end + travel)
            if prev_end is not None:
                model.Add(start >= prev_end + travel)

            prev_end = end

    # AddCumulative says: at any moment in time, the total "demand" of all
    # active intervals cannot exceed "capacity" (number of chargers).
    # Each interval has demand=1 (one bus = one charger slot).

    for station, cfg in scenario.stations.items():
        station_ivs = [intervals[k][2] for k in intervals if k[1] == station]
        if not station_ivs:
            continue
        demands = [1] * len(station_ivs)
        model.AddCumulative(station_ivs, demands, cfg.chargers)


    w = scenario.weights

    wait_vars: list = []
    wait_lookup: dict = {}        # (bus_id, station) → wait IntVar
    operator_waits: dict[str, list] = {}
    op_station_groups: dict[tuple, list] = {}   # (operator, station) → [(delay_min, sv, bus_id)]

    for bus in scenario.buses:
        for stop in bus.upcoming:
            key = (bus.id, stop.station)
            if key not in intervals:
                continue
            start, _, _ = intervals[key]

            wait = model.NewIntVar(0, horizon, f"wait_{bus.id}_{stop.station}")
            model.Add(wait == start - stop.actual_arrival_min)
            wait_vars.append(wait)
            wait_lookup[key] = wait

            operator_waits.setdefault(bus.operator, []).append(wait)
            op_station_groups.setdefault((bus.operator, stop.station), []).append(
                (stop.delay_min, start, bus.id)
            )

    n_stops = max(len(wait_vars), 1)

    def _sum_var(terms: list, name: str, per_term_bound: int):
        if not terms:
            return model.NewConstant(0)
        s = model.NewIntVar(0, per_term_bound * len(terms), name)
        model.Add(s == sum(terms))
        return s

    # S1: individual — MAX wait (minimax)
    if wait_vars:
        individual_max = model.NewIntVar(0, horizon, "ind_max")
        model.AddMaxEquality(individual_max, wait_vars)
    else:
        individual_max = model.NewConstant(0)

    # S3: network — SUM of all waits
    network_sum = _sum_var(wait_vars, "net_sum", horizon)

    # S2: operator fairness
    op_fairness_sum = model.NewConstant(0)
    if operator_waits:
        per_op_vars = []
        for op, waits in operator_waits.items():
            op_s = model.NewIntVar(0, horizon * len(waits), f"op_{op}")
            model.Add(op_s == sum(waits))
            per_op_vars.append(op_s)
        op_fairness_sum = model.NewIntVar(0, horizon * n_stops, "op_fair")
        model.Add(op_fairness_sum == sum(per_op_vars))

    # S4: intra-operator wait fairness — O(n) not O(n²)

    intra_op_group_maxes: list = []
    for (op, station), entries in op_station_groups.items():
        if len(entries) < 2:
            continue
        # Sort by delay ascending: last entry = most delayed = should wait least
        sorted_entries = sorted(entries, key=lambda x: x[0])
        group_violations = []
        for idx in range(len(sorted_entries) - 1):
            d_lo, _, bid_lo = sorted_entries[idx]       # less delayed
            d_hi, _, bid_hi = sorted_entries[idx + 1]  # more delayed
            if d_lo == d_hi:
                continue
            wait_lo = wait_lookup.get((bid_lo, station))
            wait_hi = wait_lookup.get((bid_hi, station))
            if wait_lo is None or wait_hi is None:
                continue
            # violation: more-delayed bus (hi) waited MORE than less-delayed (lo)
            viol = model.NewIntVar(0, horizon, f"intra_{op}_{station}_{idx}")
            model.Add(viol >= wait_hi - wait_lo)
            group_violations.append(viol)

        if not group_violations:
            continue
        if len(group_violations) == 1:
            intra_op_group_maxes.append(group_violations[0])
        else:
            # Take the max violation within this group — the worst ordering mistake
            group_max = model.NewIntVar(0, horizon, f"intra_max_{op}_{station}")
            model.AddMaxEquality(group_max, group_violations)
            intra_op_group_maxes.append(group_max)

    intra_operator_sum = _sum_var(intra_op_group_maxes, "intra_op_sum", horizon)

    # Scale weights and build objective
    wi = int(round(w.individual              * WEIGHT_SCALE))
    wo = int(round(w.operator                * WEIGHT_SCALE))
    wn = int(round(w.network                 * WEIGHT_SCALE))
    wp = int(round(w.intra_operator_priority * WEIGHT_SCALE))

    model.Minimize(
        wi * individual_max         # worst single-bus wait (minimax)
        + wo * op_fairness_sum      # operator-level fairness
        + wn * network_sum          # total system-wide wait
        + wp * intra_operator_sum   # within-operator delay priority
    )

    # Search strategy: try assigning the SMALLEST start values first.
    # WHY: When multiple buses arrive at a station at the same time (clusters),

    all_start_vars = [intervals[k][0] for k in intervals
                      if not isinstance(intervals[k][0], int)]
    if all_start_vars:
        model.AddDecisionStrategy(
            all_start_vars,
            cp_model.CHOOSE_FIRST,
            cp_model.SELECT_MIN_VALUE,
        )
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    solver.parameters.num_search_workers = 4
    status_code = solver.Solve(model)

    status_name = solver.StatusName(status_code)

    # If no feasible solution found, return empty schedule
    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return Schedule(
            by_station={s: [] for s in scenario.stations},
            total_wait_min=0,
            objective=float("inf"),
            status=status_name,
        )

    # ── Extract results ───────────────────────────────────────────────────
    by_station: dict[str, list[ChargingSlot]] = {s: [] for s in scenario.stations}
    total_wait = 0

    for bus in scenario.buses:
        # Include the live in-progress charge so UI shows current reality
        if bus.charging_at and bus.charging_started_min is not None:
            s_min = bus.charging_started_min
            # Find the corresponding stop's scheduled arrival (if any)
            # The live charge has no "upcoming" stop, so we use charging_started as both
            slot = ChargingSlot(
                bus_id=bus.id,
                operator=bus.operator,
                direction=bus.direction,
                station=bus.charging_at,
                start_min=s_min,
                end_min=s_min + charge,
                actual_arrival_min=s_min,   # started immediately on arrival
                scheduled_arrival_min=s_min,
                cumulative_wait_min=0,
                delay_min=0,
            )
            by_station[bus.charging_at].append(slot)

        # Extract solver values for upcoming stops
        for stop in bus.upcoming:
            key = (bus.id, stop.station)
            if key not in intervals:
                continue
            start_var, end_var, _ = intervals[key]
            s_min = int(solver.Value(start_var))
            e_min = int(solver.Value(end_var))
            curr_wait = max(s_min - stop.actual_arrival_min, 0)
            total_wait += curr_wait

            slot = ChargingSlot(
                bus_id=bus.id,
                operator=bus.operator,
                direction=bus.direction,
                station=stop.station,
                start_min=s_min,
                end_min=e_min,
                actual_arrival_min=stop.actual_arrival_min,
                scheduled_arrival_min=stop.scheduled_arrival_min,
                cumulative_wait_min=stop.cumulative_wait_min,
                delay_min=stop.delay_min,
            )
            by_station[stop.station].append(slot)

    return Schedule(
        by_station=by_station,
        total_wait_min=total_wait,
        objective=solver.ObjectiveValue(),
        status=status_name,
    )