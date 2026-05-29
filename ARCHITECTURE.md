# Architecture

## What this system does

A single-process Streamlit app that reads a scenario JSON file (a frozen snapshot of the bus network), feeds it into a CP-SAT constraint solver, and renders the resulting charging order per station in the browser.

---

## Data flow

```
scenarios/scenario_N.json
         │
         ▼
    loader.py          Parses JSON → typed Python objects
         │             Converts HH:MM strings → integer minutes
         ▼             Reads weights from JSON
    Scenario object    (model.py types)
         │
         ▼
    solver.py          Builds CP-SAT model
         │             Declares variables, adds constraints, sets objective
         │             Calls solver.Solve()
         ▼
    Schedule object    Charging slots per station, total wait, objective value
         │
         ▼
    app.py             Streamlit renders the schedule
                       Zero scheduling logic here
```

Each layer has one job. The UI never touches the constraint model. Adding a constraint never touches the UI or the JSON format.

---

## File responsibilities

### `model.py` — Pure data types

Frozen dataclasses. No logic, no I/O. Everything else imports from here.

Key types:

- `UpcomingStop` — one future charging stop for a bus. Has both `scheduled_arrival_min` (timetable) and `actual_arrival_min` (physical reality). The gap between them is `delay_min`.
- `Bus` — one bus at snapshot time. If currently charging, has `charging_at` and `charging_started_min` so the solver can model the active slot as fixed.
- `Scenario` — the complete frozen network state. Holds all buses, station configs, weights, and timing parameters.
- `Weights` — four floats controlling the objective terms.

**If the interviewer says "add a field"** → start here, then update `loader.py`.

### `loader.py` — I/O boundary

Only file that reads JSON. Converts HH:MM strings to integer minutes (all internal times are minutes from snapshot). Builds typed objects.

Key function: `_hhmm_to_min(snapshot, hhmm)` handles midnight wrap-around with a ±12h rule.

**Weights are read from the scenario JSON** — each file carries its own weights block. To change weights globally, edit `_envelope()` in `generate_scenarios.py` and regenerate.

### `solver.py` — The scheduling engine

Builds a CP-SAT model in 5 steps, solves it, extracts results.

**If the interviewer says "add a constraint"** → this is the only file to change.

### `app.py` — Display layer

Calls `_solve_cached()` (cached by file path + mtime), renders results. No scheduling logic.

**If the interviewer says "add a column to the table"** → add a key to the `rows` dict in `_render_station()`.

---

## Why CP-SAT (OR-Tools)?

| Option                | Problem                                                                               |
| --------------------- | ------------------------------------------------------------------------------------- |
| Greedy / hand-written | Every new rule requires rewriting ordering logic                                      |
| PuLP (LP)             | No native interval or cumulative resource constraints                                 |
| Z3                    | Good at SAT problems; slower for scheduling with many integer vars                    |
| **CP-SAT**            | Built for exactly this: interval scheduling, cumulative resources, integer objectives |

CP-SAT gives us three critical primitives:

- `NewIntervalVar` — a charging session is natively a time slot object
- `AddCumulative` — enforces available charger for any number of buses in one line
- `AddMaxEquality` — minimise the worst-case wait directly (minimax)

Adding a new constraint = one `model.Add(...)` call. No restructuring.

---

## The constraint model in detail

### Step 1 — Time horizon

All IntVars need upper and lower bounds. The horizon is:

```
horizon = latest_actual_arrival + charge * (n_buses + 2) + 120
```

Too small → CP-SAT can't fit all buses. Too large → search space explodes.

### Step 2 — Interval variables

For each `(bus, upcoming_station)` pair:

```python
start = model.NewIntVar(earliest, horizon, ...)  # decision variable: when charging begins
end   = model.NewIntVar(earliest, horizon, ...)  # = start + charge_minutes
iv    = model.NewIntervalVar(start, charge, end, ...)  # occupies a time slot
```

Buses **currently charging** at snapshot time → `NewConstant` (fixed intervals). The charger is already physically occupied; the solver cannot move these.

### Step 3 — Hard constraints

**H1 — Fixed duration** (built into `NewIntervalVar`):

```python
# end - start == charge_minutes always. No extra line needed.
model.NewIntervalVar(start, charge_minutes, end, name)
```

**H2 — No of Chargers** (`AddCumulative`):

```python
model.AddCumulative(all_intervals_at_station, demands=[1,...], capacity=cfg.chargers)
# At any moment in time: simultaneous charges at this station ≤ chargers
# Change cfg.chargers in the JSON to change capacity — no code change needed
```

**H3a — Physical arrival lower bound**:

```python
model.Add(start >= stop.actual_arrival_min)
# A bus cannot begin charging before it physically arrives
```

**H3b — Route precedence**:

```python
model.Add(start >= prev_end + travel_minutes_per_leg)
# A bus must finish charging at station N AND travel before charging at N+1
# CP-SAT implicitly takes max(actual_arrival, prev_end + travel)
```

### Step 4 — Soft objective

Four terms, all minimised together:

```
Objective = wi × individual_max      — max wait of any single bus (minimax)
          + wo × op_fairness_sum     — sum of per-operator total waits (operator fairness)
          + wn × network_sum         — sum of ALL waits across all buses (throughput)
          + wp × intra_operator_sum  — within-operator delay ordering violations
```

**Why individual uses `AddMaxEquality` (max), not sum:**
Sum would let the solver make one bus wait 60 minutes if it saved three buses 1 minute each. Max directly penalises the worst outlier — the right semantic for individual fairness.

**Why network is sum, not max:**
Network captures total system throughput — every idle minute across the whole fleet. Sum is the right metric here. Together with individual (max), they balance "protect the worst-off bus" vs "reduce overall waste".

**Why operator uses separate IntVars per operator:**
Building each operator's total wait as a separate variable lets CP-SAT reason about each operator independently during search, nudging toward balanced solutions. Using `AddMaxEquality` here was 5x slower (benchmarked) — the sum approach is the right trade-off.

**Why intra-operator uses consecutive sorted pairs (O(n), not O(n²)):**
Within each (operator, station) group, sort by `delay_min`. Compare only consecutive pairs in sorted order. If `wait_A ≤ wait_B` and `wait_B ≤ wait_C`, then `wait_A ≤ wait_C` is guaranteed by transitivity — no need for the (A,C) pair. This gives 12 IntVars instead of 300 for scenario 5.

**Why intra-operator compares WAITS, not start times:**
Start times include travel time differences the scheduler cannot change. A delayed bus that arrives 3 hours after a normal bus will always start charging later — that's physics, not a scheduling choice. Comparing waits (start − actual_arrival) isolates only the part the scheduler controls.

All weights are multiplied by 1000 because CP-SAT requires integer coefficients.

---

## Adding a new constraint — full example

**Requirement:** "Buses from operator KPN cannot charge at station B before 21:00"

Snapshot time is 20:45, so 21:00 = +15 minutes from snapshot.

In `solver.py`, after the `AddCumulative` block:

```python
# Hard constraint: KPN buses cannot start charging at B before 21:00
KPN_B_OPEN = 15  # minutes from snapshot = 21:00
for bus in scenario.buses:
    if bus.operator == 'kpn':
        key = (bus.id, 'B')
        if key in intervals:
            start, _, _ = intervals[key]
            model.Add(start >= KPN_B_OPEN)
```

That is the **entire change**. No other file needs updating.

To make it configurable from the JSON:

1. Add `"kpn_b_open": 15` to the scenario JSON
2. Read it in `loader.py`, store on `Scenario` as a new field
3. Reference `scenario.kpn_b_open` in the constraint above

---

## Key design decisions and their rationale

### Weights come from the scenario JSON

Each scenario file has a `"weights"` block. `loader.py` reads them at runtime. This means a fresh scenario file can carry its own weights — the solver respects them with zero code changes.

To change weights globally: edit `_envelope()` in `generate_scenarios.py` and regenerate. To change for one scenario: edit that JSON file directly.

### `actual_arrival_min` vs `scheduled_arrival_min`

Both are stored per stop. The solver uses `actual_arrival_min` as the hard lower bound (physical reality — when the bus will actually arrive). `scheduled_arrival_min` is informational — it lets us compute `delay_min = actual - scheduled` to know if a bus is late or early. Storing both in the JSON makes the file self-describing.

### Live charges are fixed intervals

A bus currently charging at snapshot time gets `model.NewConstant()` — not a variable. The charge is physically happening; it cannot be rescheduled. This ensures the scenario data and the solver's model agree on reality.

### Search strategy hint

```python
model.AddDecisionStrategy(all_start_vars, CHOOSE_FIRST, SELECT_MIN_VALUE)
```

Tells CP-SAT to try the smallest start values first. This mimics first-come-first-served as an initial guess — usually near-optimal — and prunes the symmetric search space when buses arrive at the same time.
