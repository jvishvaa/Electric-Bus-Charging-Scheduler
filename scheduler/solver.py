"""
solver.py — CP-SAT scheduling engine.

WHAT THIS FILE DOES:
  Takes a Scenario → returns a Schedule (ordered charging slots per station).

HOW CP-SAT WORKS (explain this in the interview):
  1. Declare IntVars: "start time for bus X at station Y" (unknown integer)
  2. Add HARD constraints: "this must always be true"
  3. Add SOFT objective: "minimise this weighted sum"
  4. Call solver.Solve() → it finds values for all IntVars

THE 3 KEY HARD CONSTRAINTS:
  [H1] Each charging slot has fixed duration (charge_minutes)
  [H2] Station charger capacity: AddCumulative limits simultaneous use
  [H3] Route precedence: bus must finish at station N before reaching N+1

THE SOFT OBJECTIVE (4 terms, all minimised):
  [S1] individual_sum:  sum of (start - earliest_arrival) for every slot
                        → minimises how long each bus waits
  [S2] operator_max:    max of (per-operator total wait)
                        → minimises worst-treated operator (fairness)
  [S3] network_sum:     sum of all charging end times
                        → minimises total completion time
  [S4] delay_penalty:   sum of (wait * (1 + delay_factor)) for late buses
                        → late buses get priority; early buses don't

INTERVIEW: "How do you add a new constraint?"
  Example: "No KPN bus can charge at station B before 21:00"
  → Add this after the existing constraint blocks:
    for bus in scenario.buses:
        if bus.operator == 'kpn':
            key = (bus.id, 'B')
            if key in intervals:
                start, _, _ = intervals[key]
                model.Add(start >= 15)   # 15 min after snapshot = 21:00

INTERVIEW: "What if you wanted to add bus priority?"
  → Add a 'priority' field to Bus in model.py
  → In the objective: multiply individual wait by (1 / priority) so high-
    priority buses are penalised more for waiting, making solver prefer them.

OUTPUT: Schedule
  A dataclass containing:
  - by_station: dict mapping station name → list of ChargingSlot
  - total_wait_min: total wait across all buses and all stops (for UI metric)
  - objective: raw solver objective value
  - status: "OPTIMAL" or "FEASIBLE" (feasible = time limit hit but valid)
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .model import ROUTE, Bus, Scenario

WEIGHT_SCALE = 1000   # CP-SAT requires integer coefficients; scale floats by this


# ──────────────────────────────────────────────
# Output types
# ──────────────────────────────────────────────

@dataclass
class ChargingSlot:
    """One scheduled charging session for one bus at one station.

    The UI renders this. Fields:
      start_min / end_min      → minutes from snapshot when charging starts/ends
      earliest_arrival_min     → the physical lower bound (from UpcomingStop)
      scheduled_arrival_min    → what the timetable said
      curr_wait_min            → how long bus waited at THIS station
                                 = start_min - earliest_arrival_min
      cumulative_wait_min      → wait at prior stations (from input data) +
                                 curr_wait_min (to show total journey wait)
      delay_min                → earliest - scheduled (negative=early, positive=late)
    """
    bus_id: str
    operator: str
    direction: str
    station: str
    start_min: int
    end_min: int
    earliest_arrival_min: int
    scheduled_arrival_min: int
    cumulative_wait_min: int   # wait accumulated at previous stations
    delay_min: int             # earliest - scheduled for this stop

    @property
    def curr_wait_min(self) -> int:
        """How long did this bus wait at this station before charging started?"""
        return max(self.start_min - self.earliest_arrival_min, 0)

    @property
    def total_wait_min(self) -> int:
        """cumulative from prior stations + current station wait."""
        return self.cumulative_wait_min + self.curr_wait_min


@dataclass
class Schedule:
    """Full solver output.

    INTERVIEW: "What is the difference between objective and total_wait_min?"
      objective = the raw number CP-SAT minimised (scaled, includes all
                  weighted terms mixed together — not human-readable).
      total_wait_min = plain sum of actual wait times across all slots.
                       Human-readable. Useful for comparing scenarios.
    """
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


# ──────────────────────────────────────────────
# Main solver function
# ──────────────────────────────────────────────

def solve(scenario: Scenario, time_limit_s: float = 20.0) -> Schedule:
    """
    Build the CP-SAT model and solve it.

    WALK-THROUGH (tell this story in the interview):

    Step 1 — Compute time horizon
      We need an upper bound for all IntVars. If the horizon is too small,
      CP-SAT can't fit all buses. If too large, the search space blows up.
      We set it to: latest_possible_arrival + charge * (n_buses + 1) + buffer.

    Step 2 — Create interval variables
      For each (bus, upcoming_stop), create:
        start: IntVar — when charging begins
        end:   IntVar — when charging ends (= start + charge_minutes, fixed)
        iv:    IntervalVar — a CP-SAT object that "occupies" a time slot
      If a bus is currently charging, we create FIXED intervals (constants)
      so that charger slot is already occupied at snapshot time.

    Step 3 — Hard constraints
      H1: IntervalVar has fixed duration (built into NewIntervalVar)
      H2: AddCumulative per station — total simultaneous use ≤ chargers
      H3: Route precedence — start[bus, station_i+1] ≥ end[bus, station_i] + travel

    Step 4 — Soft objective
      Four weighted terms, scaled to ints for CP-SAT.

    Step 5 — Solve and extract
      Read solver.Value(start) for each variable to get the actual schedule.
    """
    model = cp_model.CpModel()
    charge = scenario.charge_minutes
    travel = scenario.travel_minutes_per_leg

    # ── Step 1: Compute time horizon ──────────────────────────────────────
    # earliest: the minimum time in our system (could be negative if buses
    # started charging before snapshot)
    earliest_times = [0]
    for bus in scenario.buses:
        if bus.charging_started_min is not None:
            earliest_times.append(bus.charging_started_min)
        for stop in bus.upcoming:
            earliest_times.append(stop.earliest_arrival_min)
    earliest = min(earliest_times)

    latest_times = [u.earliest_arrival_min for b in scenario.buses for u in b.upcoming]
    latest_arrival = max(latest_times) if latest_times else 0

    # Upper bound: worst case = every bus queues behind every other at same station
    horizon = latest_arrival + charge * (len(scenario.buses) + 2) + 120

    # ── Step 2: Create interval variables ─────────────────────────────────
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

            start = model.NewIntVar(earliest, horizon, f"start_{bus.id}_{stop.station}")
            end = model.NewIntVar(earliest, horizon, f"end_{bus.id}_{stop.station}")
            iv = model.NewIntervalVar(
                start, charge, end,
                f"iv_{bus.id}_{stop.station}"
            )
            intervals[key] = (start, end, iv)

            # ── Hard Constraint H3a: earliest physical arrival ──────────
            # Bus cannot start charging before it physically arrives.
            # This is the fundamental lower bound from the real world.
            model.Add(start >= stop.earliest_arrival_min)

            # ── Hard Constraint H3b: route precedence ───────────────────
            # Bus must finish at previous station AND travel before arriving here.
            # model.Add(start >= prev_end + travel) means:
            #   charging can only start after the previous charge ends + travel.
            # Combined with H3a (earliest arrival), CP-SAT effectively takes
            # the max of both: start ≥ max(earliest_arrival, prev_end + travel)
            if prev_end is not None:
                model.Add(start >= prev_end + travel)

            prev_end = end

    # ── Hard Constraint H2: charger capacity per station ──────────────────
    # AddCumulative says: at any moment in time, the total "demand" of all
    # active intervals cannot exceed "capacity" (number of chargers).
    # Each interval has demand=1 (one bus = one charger slot).
    # INTERVIEW: "How would you model a fast charger that takes 2 slots?"
    #   → Set demand=2 for that bus's interval. Or add a special interval type.
    for station, cfg in scenario.stations.items():
        station_ivs = [intervals[k][2] for k in intervals if k[1] == station]
        if not station_ivs:
            continue
        demands = [1] * len(station_ivs)
        model.AddCumulative(station_ivs, demands, cfg.chargers)

    # ── Step 4: Soft objective terms ──────────────────────────────────────
    #
    # FOUR TERMS, each measuring something distinct:
    #
    # S1  individual_max        : MAX wait of any single bus at any stop (minimax)
    #     → "no one bus suffers too long"
    #
    # S2  op_fairness_sum       : per-operator total waits, summed as separate vars
    #     → "no operator's fleet collectively bears all the delay"
    #
    # S3  network_sum           : SUM of all waits across all buses and stations
    #     → "minimise total system-wide idle time"
    #
    # S4  intra_operator_sum    : within same operator at same station, if a
    #     more-delayed bus waits MORE than a less-delayed bus — penalise that
    #     → "within your own fleet, delayed buses get queue priority"
    #     → compares WAITS (not start times) so only scheduler choices count

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
            model.Add(wait == start - stop.earliest_arrival_min)
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

    # S4: intra-operator wait fairness
    # For each pair of buses from the same operator at the same station:
    # if the more-delayed bus waits MORE than the less-delayed one → penalise.
    # violation = max(0, wait_more_delayed - wait_less_delayed)
    # This is 0 when the delayed bus gets fair (or better) treatment.
    # It only fires when the scheduler actively disadvantages the delayed bus.
    intra_op_violations: list = []
    for (op, station), entries in op_station_groups.items():
        if len(entries) < 2:
            continue
        for i in range(len(entries)):
            d_i, _, bid_i = entries[i]
            wait_i = wait_lookup.get((bid_i, station))
            if wait_i is None:
                continue
            for j in range(i + 1, len(entries)):
                d_j, _, bid_j = entries[j]
                if d_i == d_j:
                    continue
                wait_j = wait_lookup.get((bid_j, station))
                if wait_j is None:
                    continue
                # More-delayed bus should wait ≤ less-delayed bus
                viol = model.NewIntVar(0, horizon, f"intra_{op}_{station}_{i}_{j}")
                if d_i > d_j:
                    # bus_i more delayed → wait_i should be ≤ wait_j
                    model.Add(viol >= wait_i - wait_j)
                else:
                    # bus_j more delayed → wait_j should be ≤ wait_i
                    model.Add(viol >= wait_j - wait_i)
                intra_op_violations.append(viol)

    intra_operator_sum = _sum_var(intra_op_violations, "intra_op_sum", horizon)

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

    # ── Step 5: Solve ─────────────────────────────────────────────────────
    # Search strategy: try assigning the SMALLEST start values first.
    # WHY: When multiple buses arrive at a station at the same time (clusters),
    # CP-SAT faces a symmetric search space (any ordering of the cluster is
    # equally valid). Without a hint it explores many equivalent branches.
    # Hinting "try small values first" mimics a first-come-first-served policy
    # as the initial guess, which is usually near-optimal and prunes the tree.
    # INTERVIEW: "How did you speed up the solver on clustered scenarios?" → this.
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
                earliest_arrival_min=s_min,   # started immediately on arrival
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
            curr_wait = max(s_min - stop.earliest_arrival_min, 0)
            total_wait += curr_wait

            slot = ChargingSlot(
                bus_id=bus.id,
                operator=bus.operator,
                direction=bus.direction,
                station=stop.station,
                start_min=s_min,
                end_min=e_min,
                earliest_arrival_min=stop.earliest_arrival_min,
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