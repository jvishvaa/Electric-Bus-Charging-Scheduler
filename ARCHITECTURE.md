# Architecture

## What was built

A single-process Streamlit app that:

1. Reads a scenario JSON file (network snapshot)
2. Feeds it into a CP-SAT constraint solver (Google OR-Tools)
3. Renders the charging order per station in the browser

## Data flow

```
scenario JSON
     ↓  loader.py  (parse + type)
  Scenario object
     ↓  solver.py  (CP-SAT model)
  Schedule object
     ↓  app.py     (Streamlit render)
  Browser UI
```

Each layer has one job. Changing the UI doesn't touch the solver. Adding a constraint doesn't touch the UI.

---

## Why CP-SAT (OR-Tools)?

| Option           | Why rejected                                                                            |
| ---------------- | --------------------------------------------------------------------------------------- |
| Greedy algorithm | Adding a new constraint = rewrite the ordering logic                                    |
| PuLP (LP solver) | No native support for interval/cumulative constraints                                   |
| Z3               | Excellent for SAT; slower on scheduling problems with many integer vars                 |
| CP-SAT           | Built for exactly this: interval scheduling + integer objectives + cumulative resources |

CP-SAT key properties for this problem:

- `NewIntervalVar` — models a charging session natively as a time-slot object
- `AddCumulative` — enforces charger capacity in one line, regardless of how many buses
- `AddMaxEquality` — lets us minimise the worst-case wait directly
- Adding a constraint = one `model.Add(...)` call, no restructuring

---

## The constraint model

### Variables

For each `(bus, upcoming_station)` pair:

```python
start = model.NewIntVar(earliest, horizon, ...)  # when charging begins
end   = model.NewIntVar(earliest, horizon, ...)  # when charging ends
iv    = model.NewIntervalVar(start, charge, end, ...)  # the slot itself
```

Buses currently mid-charge get `NewConstant` (fixed) intervals — the charger is already occupied.

### Hard constraints

**H1 — Fixed duration** (built into `NewIntervalVar`):

```python
model.NewIntervalVar(start, charge_minutes, end, name)
# end - start == charge_minutes always
```

**H2 — Charger capacity** (`AddCumulative`):

```python
model.AddCumulative(station_intervals, demands=[1,...], capacity=chargers)
# At any moment: simultaneous charges ≤ chargers
```

**H3a — Physical arrival lower bound**:

```python
model.Add(start >= stop.earliest_arrival_min)
# Can't charge before arriving
```

**H3b — Route precedence**:

```python
model.Add(start >= prev_end + travel_minutes_per_leg)
# Must finish previous station + travel before starting next
```

### Soft objective (minimised)

```
Objective = wi × individual_max        (max wait of any single bus)
          + wo × op_fairness_sum       (sum of per-operator total waits)
          + wn × network_sum           (sum of all waits — total throughput)
          + wp × intra_operator_sum    (within same operator: delayed bus waits ≤ normal bus)
```

All weights multiplied by 1000 (CP-SAT requires integer coefficients).

---

## Adding a new constraint — full example

**Requirement:** "Buses from operator KPN should not charge at station B before 21:00"

Snapshot time is 20:45, so 21:00 = +15 minutes from snapshot.

In `solver.py`, after the `AddCumulative` block (Step 3):

```python
# Hard constraint: KPN buses cannot start charging at B before 21:00
# (21:00 = snapshot 20:45 + 15 min)
KPN_B_EARLIEST = 15  # minutes from snapshot
for bus in scenario.buses:
    if bus.operator == 'kpn':
        key = (bus.id, 'B')
        if key in intervals:
            start, _, _ = intervals[key]
            model.Add(start >= KPN_B_EARLIEST)
```

That's the entire change. The solver respects it automatically — no other file needs updating.

To make it configurable (driven from scenario JSON):

1. Add `"kpn_b_earliest": 15` to the scenario JSON
2. Read it in `loader.py` → store on `Scenario`
3. Reference `scenario.kpn_b_earliest` in the constraint above

---

## Key design decisions

**Global weights, not per-scenario weights**
Weights are a system-level policy (how much we care about fairness vs throughput). They should be consistent across all snapshots. Scenario files carry a `weights` key for documentation but the solver uses `GLOBAL_WEIGHTS` from `loader.py`.

**`earliest_arrival_min` vs `scheduled_arrival_min`**
Both are stored per stop. The solver uses `earliest_arrival_min` as the hard lower bound (physical reality). `scheduled_arrival_min` is informational — it tells us whether a bus is running late or early. The `delay_min` field pre-computes `earliest - scheduled` for convenience.

**Intra-operator priority via wait comparison**
We penalise cases where a more-delayed bus waits _longer_ than a less-delayed bus from the same operator at the same station. This is a wait comparison, not a start-time comparison — start times include physical travel time differences which the scheduler cannot change. Waits isolate only the part the scheduler controls.

**`AddMaxEquality` for individual term**
Benchmarked faster than sum for clustered scenarios because it gives CP-SAT a tight upper bound to prune against. The minimax semantic is also correct: we want to protect the worst-off bus, not just reduce total wait.
