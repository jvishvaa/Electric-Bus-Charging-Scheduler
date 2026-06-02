# Bus Charging Scheduler

A scheduler for electric buses on the Bengaluru → A → B → C → D → Kochi route, built with Python, Streamlit, and Google OR-Tools CP-SAT. Given a list of bus departures, the scheduler decides each bus's charging plan (which stations it uses) and the order in which buses use each charger.

**Hosted demo:** <https://jvishvaa.streamlit.app/>

## The problem in one sentence

20 buses (10 Bengaluru→Kochi, 10 Kochi→Bengaluru) share 4 charging stations along a 540 km route. Battery range is 240 km, every charge takes 25 minutes and fills to full, each station has 1 charger. Decide who charges where, and when.

## Companion documents

| Document           | What's in it                                                                                                    |
| ------------------ | --------------------------------------------------------------------------------------------------------------- |
| `PRODUCT.md`       | Why this product exists, who it's for, what it does and doesn't do today, how success is measured.              |
| `ARCHITECTURE.md`  | Framework choice (CP-SAT) and why, data-model design, anticipated changes, how to add a rule, assumptions made. |
| `README.md` (here) | How to run it, how to change a weight, project layout, the 5 supplied scenarios.                                |

## Quick start

```bash
pip install -r requirements.txt
python generate_scenarios.py    # only needed if you edited the generator
streamlit run app.py
```

Open <http://localhost:8501>, pick a scenario, see the schedule.

Python 3.11+ is recommended. Dependencies are pinned in `requirements.txt`; Streamlit Community Cloud installs them automatically on deploy.

## Project structure

```
bus-charging-scheduler/
├── app.py                      Streamlit UI (no scheduling logic)
├── generate_scenarios.py       Emits the 5 scenario JSON files
├── requirements.txt
├── PRODUCT.md                  Product overview — what / why / for whom
├── ARCHITECTURE.md             Engineering deep-dive — framework, model, trade-offs
├── README.md                   This file — operational quick start
├── scenarios/
│   ├── scenario_1_even_spacing.json
│   ├── scenario_2_bunched_start.json
│   ├── scenario_3_asymmetric_load.json
│   ├── scenario_4_operator_heavy.json
│   └── scenario_5_worst_case.json
└── scheduler/
    ├── model.py                Frozen dataclasses — Scenario, Bus, Route, Weights
    ├── loader.py               JSON → typed objects
    └── solver.py               CP-SAT model → returns Schedule
```

Each layer has exactly one job. See `ARCHITECTURE.md` for the rationale and data flow.

## The 5 scenarios

| #   | Name            | What it tests                                                             |
| --- | --------------- | ------------------------------------------------------------------------- |
| 1   | Even spacing    | Baseline — buses depart every 15 min from each end.                       |
| 2   | Bunched start   | Tight 8-min cluster early, then spaces out. Heavy early contention.       |
| 3   | Asymmetric load | 10 BK vs 4 KB. Uneven traffic across directions.                          |
| 4   | Operator-heavy  | KPN runs 8 of 10 BK buses. Operator weight = 2.0 — visible policy effect. |
| 5   | Worst case      | All 20 buses inside 72 minutes. Convergence at inner stations.            |

## How to change the world without changing code

Everything physical and every weight lives in the scenario JSON.

| To change...                                | Edit...                                       |
| ------------------------------------------- | --------------------------------------------- |
| Battery range                               | `battery_range_km` in the JSON                |
| Charge duration                             | `charge_minutes`                              |
| Travel speed                                | `speed_kmph`                                  |
| Add or remove a station                     | `route.nodes`, `route.segments`, `stations`   |
| Charger count at one station                | `stations.<name>.chargers`                    |
| Tune weights                                | `weights.individual` / `operator` / `overall` |
| Add a bus / cancel a bus / change departure | `buses` array                                 |

To use a brand-new scenario: drop a JSON file into `scenarios/`. The UI picks it up on the next reload.

## How to change a weight

Open the scenario JSON and edit the `weights` block:

```jsonc
"weights": {
  "individual": 1.0,   // worst single-bus wait (minimax)
  "operator":   2.0,   // worst per-operator total wait (minimax — fairness)
  "overall":    1.0    // sum of all waits (network throughput)
}
```

Save, reload the app — the new schedule reflects the new weights.

## How to add a new rule

See **Adding a new hard rule** and **Adding a new soft rule** in `ARCHITECTURE.md` — both are 5–10 lines of Python in `solver.py`, no other file changes.

## How to add a new weight

1. Add the field to `Weights` in `scheduler/model.py`.
2. Read it in `scheduler/loader.py`.
3. Build the matching term in `solver.py`'s `Minimize(...)`.
4. Set the value in each scenario JSON.

## What's in the doc but not modelled

- **Endpoint slow-charging** — buses always start with full range, as the doc states. We don't model the slow-charging hardware at Bengaluru/Kochi because the buses simply leave with a 240 km range.
- **Multiple chargers per station** — the model supports it (set `chargers > 1` in the JSON, the solver switches from `NoOverlap` to `Cumulative`), but the supplied scenarios all use 1 as the doc specifies.

## Submission checklist

- [x] Hosted Streamlit app — <https://jvishvaa.streamlit.app/>
- [x] All 5 scenarios encoded (`scenarios/*.json`)
- [x] `PRODUCT.md`, `README.md`, `ARCHITECTURE.md`
- [x] Scheduler is data-driven; new world shape = new JSON, no code change
- [x] Weights are tunable in one obvious place (the scenario JSON)
