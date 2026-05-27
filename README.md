# Bus Charging Scheduler

A constraint-based scheduler for electric buses that share charging stations along a fixed route.

## Quick start

```bash
pip install -r requirements.txt
python generate_scenarios.py   # creates scenarios/ folder
streamlit run app.py
```

Open http://localhost:8501 — pick a scenario from the dropdown, see the schedule.

## Project structure

```
app.py                    — Streamlit UI (rendering only, no logic)
generate_scenarios.py     — Creates the 5 scenario JSON files
requirements.txt
scenarios/                — 5 scenario JSON files
scheduler/
    model.py              — Typed data model (Scenario, Bus, UpcomingStop, Weights)
    loader.py             — Parses JSON → model objects; holds GLOBAL_WEIGHTS
    solver.py             — CP-SAT constraint model; returns Schedule
```

---

## How to run locally

```bash
git clone <repo>
cd bus_scheduler
pip install -r requirements.txt
streamlit run app.py
```

Scenarios are already committed. If you want to regenerate them:

```bash
python generate_scenarios.py
```

---

## How to add a new scenario

1. Open `generate_scenarios.py`
2. Copy any existing builder function (e.g. `scenario_1_baseline`) and rename it
3. Change the perturbation logic (see comments in the file)
4. Add it to the `builders` dict in `main()`
5. Run `python generate_scenarios.py`
6. The new file appears in `scenarios/` and the UI dropdown picks it up automatically

Example — add "Station C charger down":

```python
def scenario_6_charger_failure() -> dict:
    starts = _staggered_starts(40, seed=6)
    dirs = _alternating_directions()
    buses = _make_fleet("s6", starts, dirs, lambda i, a, b: 0)
    return _envelope(
        "Scenario 6 - Station C charger failure",
        buses,
        chargers={"A": 2, "B": 2, "C": 1, "D": 2}  # C is down to 1
    )
```

---

## How to change a weight

Weights are global constants in `scheduler/loader.py`:

```python
GLOBAL_WEIGHTS = Weights(
    individual=1.5,              # worst single-bus wait (minimax)
    operator=1.0,                # operator fairness
    network=0.5,                 # total system wait
    intra_operator_priority=0.8, # within-operator delay ordering
)
```

Change any number and restart the app. The cache clears automatically.

---

## How to add a new constraint

All constraints live in `scheduler/solver.py`, in the clearly marked blocks:

**Hard constraint** (must always hold):

```python
# Example: no bus from operator 'kpn' can charge at station B before 21:00
# Add after the existing hard constraint blocks (Step 3):
for bus in scenario.buses:
    if bus.operator == 'kpn':
        key = (bus.id, 'B')
        if key in intervals:
            start, _, _ = intervals[key]
            model.Add(start >= 15)  # 15 min after snapshot 20:45 = 21:00
```

**Soft constraint** (preference, not a rule):

```python
# Example: penalise buses that have been waiting since the previous station
# Add a new term to the objective:
fairness_terms = [r["wait"] for r in wait_records if r["stop"].cumulative_wait_min > 30]
fairness_sum = _sum_var(fairness_terms, "fairness", horizon)
# Add `+ w_fairness * fairness_sum` to model.Minimize(...)
```

See `ARCHITECTURE.md` for a full walkthrough.

---

## How to change charge time or travel time

These are parameters in each scenario JSON file:

```json
{
  "charge_minutes": 15,
  "travel_minutes_per_leg": 150
}
```

Change them in the JSON and reload the scenario. No code changes needed.

---

## How to change charger counts per station

In the scenario JSON:

```json
"stations": {
  "A": { "chargers": 2 },
  "B": { "chargers": 1 },
  ...
}
```

The solver's `AddCumulative` constraint reads this value directly.
