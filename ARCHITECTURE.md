# Architecture

## What this system does

A single-process Streamlit app reads a scenario JSON file (route + buses + departure times + weights), feeds it into a CP-SAT constraint solver, and renders the per-bus schedule and per-station charging order.

The scheduler decides two things for every bus:

1. **Which** stations the bus uses (any subset of A/B/C/D such that no leg between charges exceeds the battery range).
2. **When** it charges at each — i.e. the order in which buses use each charger.

---

## Data flow

```
scenarios/scenario_N.json
         │
         ▼
    loader.py        Parses JSON → typed Python objects
         │           Converts HH:MM strings to integer minutes
         ▼
    Scenario (model.py)
         │
         ▼
    solver.py        Builds CP-SAT model: optional intervals, range
         │           constraint, charger capacity, soft objective
         ▼
    Schedule         Per-bus timeline + per-station ordering
         │
         ▼
    app.py           Streamlit renders the schedule
                     Zero scheduling logic here
```

Each file has one job. The UI never touches the constraint model. Adding a constraint never touches the UI or the JSON format.

---

## File responsibilities

### `scheduler/model.py` — pure data types

Frozen dataclasses, no logic, no I/O:

- `Segment`, `Route` — the road graph (nodes, distances, endpoints).
- `StationConfig` — one inner station, with its charger count.
- `Bus` — id, operator, direction, departure-time-in-minutes.
- `Weights` — three floats: `individual`, `operator`, `overall`.
- `Scenario` — everything bundled, plus the original JSON for the UI.

### `scheduler/loader.py` — I/O boundary

Only file that touches JSON. Converts HH:MM to minutes-from-`reference_time` (signed; ±12h disambiguation for midnight wrap).

### `scheduler/solver.py` — the engine

Builds the CP-SAT model; returns a `Schedule`.

### `app.py` — display

Calls `_solve_cached(...)`. Renders four blocks: scenario view, summary, per-bus timetable, per-station charging order.

### `generate_scenarios.py` — emits the 5 scenario files

The scenarios in the assessment doc are encoded verbatim here (bus ids, operators, directions, departure times). One function per scenario; `_envelope()` carries the shared route/range/charge/speed defaults.

---

## 2. Mathematical Framework Selection & Solver Trade-offs

The scheduling engine uses **Google OR-Tools CP-SAT**. CP-SAT was selected after evaluating a comprehensive matrix of open-source solvers, commercial mathematical programming suites, and heuristic frameworks against the project's strict single-process Python constraints.

The table below outlines the architectural trade-offs and structural reasons for selecting or rejecting each alternative framework class:

| Optimization Framework / Specific Solver                                          | Structural Strengths                                                                                                                                                                                                                                                                                                                                    | Engineering Limitations & Rejection Criteria                                                                                                                                                                                                                                                                                  |
| :-------------------------------------------------------------------------------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Greedy / Rule-Based Heuristics** <br> _(Custom Python loops, Dispatcher rules)_ | • Execution takes milliseconds.<br>• Trivial to implement initial draft scripts.                                                                                                                                                                                                                                                                        | • **Rejected.** Fails to guarantee global balancing across multi-operator systems.<br>• Highly fragile; adding a single operational rule (e.g., time-of-day pricing) requires completely rewriting the sorting and priority heuristics.                                                                                       |
| **Open-Source MILP Solvers** <br> _(COIN-OR CBC, GLPK, HiGHS)_                    | • Fully open-source (permissive licenses).<br>• Decent performance on simple, decoupled allocation matrices.                                                                                                                                                                                                                                            | • **Rejected.** Lack native interval or cumulative scheduling primitives.<br>• Modeling time-dependent capacity overlaps requires either strict time-discretization (creating a variable for every minute) or thousands of binary Big-M tracking variables, tanking scale performance.                                        |
| **Commercial MILP Suites** <br> _(Gurobi, IBM CPLEX, FICO Xpress)_                | • Unmatched execution speeds for purely continuous or mixed-integer convex linear matrices.<br>• World-class branch-and-cut engines.                                                                                                                                                                                                                    | • **Rejected.** Suffers from the same Big-M variable explosion as open-source MILP for interval-overlap logic ($O(N^2)$ binary variables for bus pairings).<br>• Requires expensive commercial licensing and introduces complex deployment overhead inside lightweight container environments like Streamlit Community Cloud. |
| **General Theorem Provers / SMT** <br> _(Microsoft Z3 Solver)_                    | • Exceptionally powerful for verifying exact logical compliance and finding boundary edge cases.                                                                                                                                                                                                                                                        | • **Rejected.** Extremely poor at driving optimization loops down to true mathematical minimum bounds for multi-term soft objectives.                                                                                                                                                                                         |
| **Local Search / Metaheuristics** <br> _(Timefold / OptaPlanner)_                 | • Elite performance for massive-scale vehicle routing and complex timetabling using Tabu Search and Simulated Annealing.                                                                                                                                                                                                                                | • **Rejected.** Java/JVM ecosystem dependencies break the absolute constraint of running a single-process, pure Python deployment.<br>• Cannot mathematically _prove_ absolute optimality or absolute infeasibility—key validation criteria for this scheduling assessment.                                                   |
| **Genetic Algorithms (GA)** <br> _(Python DEAP framework, custom GA)_             | • Massive flexibility; can optimize non-linear or arbitrary black-box cost equations.                                                                                                                                                                                                                                                                   | • **Rejected.** Highly constrained search spaces act as "needles in a haystack" for random mutations. Most mutations yield invalid schedules (violating battery range), requiring expensive repair heuristics that stall convergence.                                                                                         |
| **Traditional CP Solvers** <br> _(Gecode, Choco Solver)_                          | • Native support for finite-domain integer constraints and basic scheduling intervals.                                                                                                                                                                                                                                                                  | • **Rejected.** Rely on older filtering and chronological backtracking search trees without modern SAT conflict-driven clause learning, making them orders of magnitude slower on dense scheduling matrices.                                                                                                                  |
| **IBM ILOG CP Optimizer** <br> _(Commercial Constraint Programming)_              | • The gold standard for enterprise scheduling.<br>• Outstanding native `intervalVar` and `cumulative` functions.                                                                                                                                                                                                                                        | • **Viable Alternative.** Structurally perfect for this problem, but rejected due to commercial licensing restrictions, closed-source footprint, and heavier setup footprints compared to OR-Tools.                                                                                                                           |
| **Google OR-Tools CP-SAT**                                                        | • **SELECTED.** Combines the exact scheduling primitives of traditional CP (`IntervalVar`, `AddCumulative`) with a state-of-the-art **SAT-based Lazy Clause Generation (LCG)** solver engine.<br>• Permissive Apache 2.0 open-source license.<br>• Lightweight, pure-pip Python binding that deploys flawlessly in a single-process Streamlit pipeline. | _Architectural Fit:_ By mapping the interval boundaries directly into a SAT conflict-learning framework, it can prune millions of unfeasible scheduling branches instantly, allowing it to prove mathematical optimality for our 20-bus fleet in fractions of a second.                                                       |

CP-SAT gives us four primitives we use directly:

- `NewOptionalIntervalVar` — a charging session that may or may not occur.
- `AddNoOverlap` / `AddCumulative` — charger capacity in one line.
- `AddMaxEquality` — minimax (worst-case wait, worst-case operator).
- Integer linear constraints — for the battery-range cover.

---

## The model in detail

### Decision variables (per bus, per inner station `s`)

- `x[(bus, s)] ∈ {0,1}` — does this bus charge here?
- `start[(bus, s)]`, `end[(bus, s)]` — when charging starts/ends; `end == start + charge_minutes`.
- `interval[(bus, s)]` — `NewOptionalIntervalVar(start, charge, end, x)`.
- `arrive[(bus, s)]`, `leave[(bus, s)]`, `wait[(bus, s)]` — derived per-station times.
- `final_arrival[bus]` — when the bus reaches its destination endpoint.

`leave == end` if the bus charges, else `leave == arrive`. `arrive` of the next station = `leave + travel_minutes_for_segment`.

### Hard constraints

**H1 — Fixed charge duration.** Built into `NewOptionalIntervalVar`.

**H2 — Charger capacity.** For each station, `AddNoOverlap` (1 charger) or `AddCumulative` (k chargers).

**H3 — Causality.** `start ≥ arrive` (only enforced if `x` is true). `arrive` is computed forward from the previous `leave`, so route order is implicit.

**H4 — Battery range.** For every bus, for every pair `(i,j)` of points on its path (origin, stations, destination) where `distance(i,j) > range`:

```
x_i + x_j  ≤  1 + Σ x_k     for i < k < j
```

(With `x_origin = x_destination = 1` baked in as constants.) This is the classic _covering inequality_ — at least one station between any two "fully charged" points must be used. Exhaustive over `(i,j)`, but the pair count is tiny (≤ 6×7/2 for our route) and CP-SAT handles it instantly.

### Soft objective — three weighted terms

The doc names three things to optimize: individual, operator, overall.

For each bus we define **controllable cost** = total wait + total time charging (= 25 × number of charges). Travel time is fixed by physics, so we don't include it.

```
Objective = wᵢ × individual_max         max controllable cost across all buses
          + w_o × operator_max          max controllable cost across operators
          + w_n × overall_sum            sum of controllable cost across all buses
```

- `individual_max` uses `AddMaxEquality` — directly attacks the worst single bus, not the average. Sum would let the solver punish one bus to save many.
- `operator_max` uses `AddMaxEquality` over per-operator sums — keeps any one operator's fleet from absorbing the contention.
- `overall_sum` is a plain sum — captures total system throughput.

Weights are read from the scenario JSON. CP-SAT requires integer coefficients, so each weight is multiplied by `WEIGHT_SCALE = 1000` before entering the objective.

### Search strategy

`AddDecisionStrategy(all_starts, CHOOSE_FIRST, SELECT_MIN_VALUE)` — try the smallest start values first. With multiple buses arriving at the same time this mimics first-come-first-served and prunes symmetric search states.

---

## Adding a new hard rule

> _Example: KPN buses cannot charge at station B before 21:00._

Reference time is 19:00, so 21:00 = +120 minutes.

In `solver.py`, after the charger-capacity block:

```python
KPN_B_OPEN = 120
for bus in scenario.buses:
    if bus.operator == "kpn":
        key = (bus.id, "B")
        if key in start:
            model.Add(start[key] >= KPN_B_OPEN).OnlyEnforceIf(x[key])
```

One block. Nothing else changes.

To make it data-driven, add `"kpn_b_open_min": 120` to the scenario JSON, surface it on `Scenario` via `loader.py`, and reference it in the constraint above. Now changing the cutoff for one scenario is a JSON edit.

---

## Adding a new soft rule

> _Example: penalise charging at station C between 23:00–00:00 (electricity price spike)._

Add a new variable that counts the offending charges and a new term to `Minimize(...)`:

```python
peak_terms = []
for bus in scenario.buses:
    key = (bus.id, "C")
    if key in start:
        in_peak = model.NewBoolVar(f"peak_{bus.id}")
        # 23:00 = +240 min; 00:00 = +300 min
        model.Add(start[key] >= 240).OnlyEnforceIf(in_peak, x[key])
        model.Add(start[key] <  300).OnlyEnforceIf(in_peak, x[key])
        peak_terms.append(in_peak)

w_peak = int(round(scenario.weights.peak_price * WEIGHT_SCALE))    # add to Weights
model.Minimize(... + w_peak * sum(peak_terms))
```

The pattern is always: declare a variable that _measures_ the thing, scale it by a weight from the JSON, add it to `Minimize`. The engine doesn't change.

---

## Anticipated changes — and how this design absorbs them

The product spec is a starting point. Below are the changes we expect, and the file(s) each one touches.

| Change                                                           | Files touched                                                                            | Why it works                                                                                                                               |
| ---------------------------------------------------------------- | ---------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| Add a 5th charging station                                       | scenario JSON only                                                                       | `Route.nodes`, `route.segments`, `stations` all live in JSON. Solver iterates `scenario.stations`, no hardcoded names.                     |
| Move from 1 charger/station to 3 chargers at C                   | scenario JSON only                                                                       | `chargers: 3` switches the constraint from `AddNoOverlap` to `AddCumulative` automatically.                                                |
| Different battery range per scenario                             | scenario JSON only                                                                       | `battery_range_km` is read on load; range constraint rebuilt on every solve.                                                               |
| Different charge duration per scenario (or per bus, later)       | scenario JSON only (or add a `charge_minutes` field per bus and read it in `loader.py`). | All interval durations come from the scenario, never hardcoded.                                                                            |
| New direction (e.g. a third route segment loop)                  | scenario JSON only                                                                       | `direction` is a free-form string; `Route.path(direction)` is the only place it's interpreted.                                             |
| New operator                                                     | scenario JSON only                                                                       | Operators are strings; the solver groups by operator dynamically.                                                                          |
| Reweight or add a new weight                                     | `Weights` dataclass + `solver.Minimize(...)`                                             | One field, one term.                                                                                                                       |
| Priority buses (express services that should jump the queue)     | New `priority` field on `Bus`; new soft term                                             | Same recipe as the peak-price example above.                                                                                               |
| Time-of-day electricity costs                                    | New per-station `prices` block in JSON; new soft term                                    | Solver reads prices, computes a cost var per charge, weights it.                                                                           |
| Driver-shift constraints (a bus must complete its trip by HH:MM) | New `must_arrive_by` per bus; one hard `model.Add(final_arrival[bus] <= deadline)`       | Single line; uses the existing `final_arrival` variable.                                                                                   |
| Multiple routes sharing the same stations                        | Make `route` a list; one `Route` per direction-set; bus.direction selects                | Solver iterates whatever's in `scenario.route`; cumulative/no-overlap aggregates across routes per station automatically.                  |
| Pre-emptable charges (charge less than full)                     | New `charge_minutes` IntVar bounded by `[min, max]` per bus-station                      | Replace fixed-duration interval with variable-duration. Other constraints unchanged.                                                       |
| Bus enters mid-route (already partially through trip)            | Add `start_node` + `start_minute` to bus                                                 | Path geometry is computed in `_build_paths` — adapt to start at a non-endpoint.                                                            |
| Heterogeneous fleet (different ranges per bus)                   | Add `range_km` to `Bus`; remove the scenario-wide constant                               | Range cover constraint already iterates per-bus; just swap `R` for `bus.range_km`.                                                         |
| 200 buses instead of 20                                          | none                                                                                     | CP-SAT scales fine to hundreds of intervals; if it gets slow, raise `time_limit_s`, the search strategy already prunes the symmetric tail. |

The general rule: **anything physical lives in the JSON; anything that's a preference is a weight; anything else is one new constraint.** None of those touch the UI or the engine's structure.

---

## Key design decisions

### Each scenario carries its own route + range + charge + speed

We could put these in a shared `defaults.json`. We chose per-scenario because the assessment doc explicitly says "the world will grow" — multiple routes sharing stations, different battery ranges. Per-scenario means a new world is one new file with no edits anywhere else.

### Reference time is just a display anchor

The solver works in _minutes from reference_. Reference is "19:00" in the supplied scenarios but it doesn't have to be — sliding it would shift every HH:MM in the output without changing the schedule. This avoids any timezone or wall-clock ambiguity inside the solver.

### Wait + charge time are the controllable cost; travel is excluded

A bus going Bengaluru→Kochi covers 540 km regardless of who schedules it. The solver can only affect `wait` (queue time) and `charge_minutes × #charges` (how many recharges to do). Putting _only_ these in the per-bus cost gives the objective a clean meaning: "minimise the controllable inefficiency."

### `NewOptionalIntervalVar` rather than always-on intervals

Buses choose their station set, so each charging session is _optional_. CP-SAT's optional intervals are exactly the right primitive — when `x = 0`, the interval is dropped from the no-overlap/cumulative constraints automatically.

### Range constraint as a covering inequality, not as path enumeration

We could enumerate every legal subset of stations (there are only 8 for a 540-km trip with 4 inner stations) and force the bus to pick one. The covering inequality is cleaner: it scales to any number of stations, doesn't pre-compute subsets, and doesn't bias the search toward any particular cover.
