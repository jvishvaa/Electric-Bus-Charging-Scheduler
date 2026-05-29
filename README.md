# Bus Charging Scheduler

An electric bus charging scheduler built with Python, Streamlit, and Google OR-Tools CP-SAT. Given a snapshot of the bus network — where every bus is, what it's doing, where it's heading — it decides the order in which buses should charge at each station to minimise wait time and maintain fairness across operators.

---

## The problem in one sentence

Four charging stations (A → B → C → D), 40 buses travelling in both directions, 2 chargers per station. When multiple buses arrive at the same time, who charges first?

---

## Quick start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Scenarios are already in the repo. To regenerate them:
python generate_scenarios.py

# 3. Run the app
streamlit run app.py
```

Open **http://localhost:8501**, pick a scenario from the dropdown, and see the charging order at each station.

---

## Project structure

```
bus_scheduler/
│
├── app.py                    UI — Streamlit frontend (zero scheduling logic)
├── generate_scenarios.py     Generates the 5 scenario JSON files
├── requirements.txt
│
├── scenarios/                5 scenario JSON files (network snapshots)
│   ├── scenario_1_baseline.json
│   ├── scenario_2_traffic_block.json
│   ├── scenario_3_late_starters.json
│   ├── scenario_4_early_arrivals.json
│   └── scenario_5_mixed_reality.json
│
└── scheduler/
    ├── model.py              Pure data types — Scenario, Bus, UpcomingStop, Weights
    ├── loader.py             Parses scenario JSON → typed model objects
    └── solver.py             CP-SAT constraint model → returns Schedule
```

Each layer has exactly one job:

- `model.py` — data shapes only, no logic
- `loader.py` — I/O only, reads JSON and builds typed objects
- `solver.py` — scheduling logic only, no file I/O, no UI
- `app.py` — display only, calls `solve()` and renders results

---

## The 5 scenarios

| Scenario                        | What it tests                   | Total wait | Max single wait | Solve time |
| ------------------------------- | ------------------------------- | ---------- | --------------- | ---------- |
| 1 — Baseline                    | Clean network, no surprises     | 16 min     | 3 min           | 0.04s      |
| 2 — Traffic block B↔C (gradual) | Delayed wave hitting B and C    | 79 min     | 8 min           | 0.20s      |
| 3 — Late starters               | Clashing buses at every station | 170 min    | 9 min           | 1.5s       |
| 4 — Early arrivals              | Out-of-order queue jumping      | 313 min    | 38 min          | 0.31s      |
| 5 — Mixed reality               | Real-world messy evening        | 262 min    | 38 min          | 0.65s      |

All scenarios solve to **OPTIMAL** (CP-SAT proves no better solution exists).

---

## How to add a new scenario

1. Open `generate_scenarios.py`
2. Copy any existing builder function and rename it:

```python
def scenario_6_charger_failure() -> dict:
    """Station C drops to 1 charger — hardware fault."""
    starts = _staggered_starts(40, seed=6)
    dirs = _alternating_directions()
    buses = _make_fleet("s6", starts, dirs, lambda i, a, b: 0)
    return _envelope(
        "Scenario 6 - Station C charger failure",
        buses,
        chargers={"A": 2, "B": 2, "C": 1, "D": 2}  # C down to 1
    )
```

3. Add it to the `builders` dict in `main()`
4. Run `python generate_scenarios.py`
5. The new file appears in `scenarios/` and the UI picks it up automatically

**Or** drop any hand-written `scenario_*.json` file into `scenarios/` — the UI will show it without any code change.

---

## How to change weights

Weights live in `_envelope()` inside `generate_scenarios.py`. They are written into every scenario JSON when you run the generator:

```python
"weights": {
    "individual": 1.5,              # worst single-bus wait (minimax)
    "operator": 1.0,                # fairness across operators
    "network": 0.5,                 # total system-wide wait
    "intra_operator_priority": 0.8, # within-fleet delay ordering
}
```

**Change weights for ALL scenarios:** edit the dict above, run `python generate_scenarios.py`.

**Change weights for ONE scenario:** edit the `"weights"` block directly in that scenario's JSON file. The loader reads weights from the JSON at runtime — no code change needed, just reload the scenario in the app.

---

## How to add a new constraint

All constraints live in `scheduler/solver.py` in clearly marked blocks.

**Hard constraint** — must always hold. Add after the `AddCumulative` block (Step 3):

```python
# Example: KPN buses cannot charge at station B before 21:00
# 21:00 = snapshot 20:45 + 15 minutes
for bus in scenario.buses:
    if bus.operator == 'kpn':
        key = (bus.id, 'B')
        if key in intervals:
            start, _, _ = intervals[key]
            model.Add(start >= 15)
```

**Soft constraint** — preference, penalised but not enforced. Add a new term to `model.Minimize(...)`:

```python
# Example: penalise buses that have already waited a long time at prior stations
burden_terms = [
    wait_lookup[(bus.id, stop.station)]
    for bus in scenario.buses
    for stop in bus.upcoming
    if stop.cumulative_wait_min > 20 and (bus.id, stop.station) in wait_lookup
]
burden_sum = _sum_var(burden_terms, "burden", horizon)
w_burden = 600

model.Minimize(
    wi * individual_max
    + wo * op_fairness_sum
    + wn * network_sum
    + wp * intra_operator_sum
    + w_burden * burden_sum   # ← add this line
)
```

See `ARCHITECTURE.md` for a full explanation of how the constraint model works.

---

## How to change charge time or travel time

Edit the values in the scenario JSON file:

```json
{
  "charge_minutes": 15,
  "travel_minutes_per_leg": 150
}
```

The solver reads these directly — no code changes needed.

---

## How to change charger counts per station

Edit the scenario JSON:

```json
"stations": {
  "A": { "chargers": 2 },
  "B": { "chargers": 3 },
  "C": { "chargers": 1 },
  "D": { "chargers": 2 }
}
```

The `AddCumulative` constraint in `solver.py` reads `cfg.chargers` directly. Change it in the JSON and reload — the solver enforces the new limit automatically.

---

## How to run a fresh scenario

1. Drop the JSON file into the `scenarios/` folder
2. Select it from the dropdown in the app
3. The solver runs immediately and shows the charging order

The scenario file must follow the same format as the existing ones — `snapshot_time`, `stations`, `buses` with `upcoming` stops each having `scheduled_arrival`, `actual_arrival`, `delay_min`, `cumulative_wait_min`.
