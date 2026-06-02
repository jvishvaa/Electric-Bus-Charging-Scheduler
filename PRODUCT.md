# Product Overview — Bus Charging Scheduler

A decision-support tool for electric intercity bus fleets that share a small number of charging stations along a fixed route. Given the day's departure schedule across all operators, the scheduler decides — for every bus — _which_ stations it charges at and _in what order_ the chargers are used, while honouring physical constraints (battery range, charger occupancy) and balancing three operational priorities (per-bus delay, per-operator fairness, total network throughput).

The product is delivered as a single-process Streamlit web app. The hosted version is available at **<https://jvishvaa.streamlit.app/>**.

---

## 1. The problem we solve

Operating an electric bus on a long route is fundamentally different from operating a diesel one. A diesel bus carries enough fuel for the full trip; an electric bus does not. With a 240 km battery range on a 540 km route, every bus must charge **at least twice** before reaching its destination.

That introduces three new operational questions that simply do not exist for diesel fleets:

1. **Where should each bus charge?** Stations are limited (4 today), shared across operators, and have only one charger each.
2. **When does each bus get its turn?** Two buses arriving at the same station within 25 minutes of each other cannot both charge — one waits.
3. **How do we balance competing priorities?** Should a single late bus dominate planning, or do we instead protect operator fairness, or total system throughput?

Today these questions are answered manually by dispatchers using spreadsheets and tribal knowledge. That works for 4 stations and 20 buses. It will not work for 40 stations, 500 buses, multiple routes, time-of-day electricity pricing, and priority express services — all of which are explicitly on the roadmap.

This product answers those questions automatically, optimally, and reproducibly.

---

## 2. Who this is for

| Persona                             | What they get from the product                                                                                    |
| ----------------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| **Fleet Operations Engineer**       | A daily charging plan they can hand to drivers, with provable bounds on wait times and charger utilisation.       |
| **Operations Manager (Operator)**   | Visibility into how their fleet is scheduled relative to other operators sharing the same infrastructure.         |
| **Network Planner / Product Owner** | A simulator: feed in a hypothetical departure schedule and see whether the existing infrastructure can absorb it. |
| **Policy Engineer**                 | A weight-tuning surface — change one number, get a different (defensible) schedule, compare them side by side.    |

---

## 3. What the user does

The end-to-end user journey, today:

1. **Open the app.** Land on the scenario picker.
2. **Pick a scenario.** Five departure schedules ship with the product, covering even spacing, bunched starts, asymmetric direction loads, single-operator dominance, and worst-case convergence.
3. **Read the input.** A scenario panel shows route geometry, infrastructure parameters, weights, and the raw departure queue.
4. **Inspect the schedule.** Three views answer three different questions:
   - **System Performance Analytics** — total wait, mean wait, solver runtime, system stress index, per-operator delay totals.
   - **Fleet Timetable View** — for each bus: which stations it uses, when it arrives, when it charges, how long it waited, when it reaches the destination.
   - **Charging Node Infrastructure Queues** — for each station: the chronological order of buses, with arrival, hook-up, disconnection, and queue wait.
5. **Tune.** Edit a weight in the scenario file, reload, compare. Different weights produce visibly different schedules — that is the point.

There is no authentication, no database, no installation. Open the URL, pick a scenario.

---

## 4. Product principles

### 4.1 Schedules must be defensible

Every scheduling decision must be traceable to a hard rule (range, charger capacity) or a soft weight (individual / operator / overall). The product never produces a "magic" schedule with no explanation.

### 4.2 Weights are policy, not code

Weights live in scenario data files. Changing a weight is a one-line edit. The engine itself is policy-neutral.

### 4.3 The world will grow — the engine should not

The data model anticipates: more stations, more chargers per station, more operators, multiple routes, priority buses, time-of-day pricing, driver shifts, heterogeneous fleets, and partial-charge sessions. Each of these is absorbed by extending the scenario file or adding one rule — never by rewriting the engine. The full list lives in `ARCHITECTURE.md`.

### 4.4 One process, one repo

Scheduling logic, scenario loading, and UI all run in a single Python process. There is no microservice boundary, no message queue, no database. Everything is in-memory state.

---

## 5. Capabilities — today vs roadmap

| Capability                                                | Status  | Notes                                                                                      |
| --------------------------------------------------------- | ------- | ------------------------------------------------------------------------------------------ |
| Multi-station scheduling (4 stations)                     | Shipped | Adding a 5th is a JSON edit.                                                               |
| Multi-operator awareness (3 operators)                    | Shipped | New operator = a string in the JSON. No code change.                                       |
| Bidirectional traffic                                     | Shipped | Both directions share the same chargers via `AddNoOverlap`.                                |
| Tunable soft objectives (individual / operator / overall) | Shipped | Weights live in the scenario file; engineers edit them freely.                             |
| Multi-charger stations (k chargers per station)           | Shipped | Set `chargers > 1` in the JSON; solver switches to `AddCumulative` automatically.          |
| Provable optimality / infeasibility                       | Shipped | CP-SAT proves an optimal schedule exists or returns INFEASIBLE for the supplied scenarios. |
| Priority buses (express service jumps the queue)          | Roadmap | One new soft term — pattern documented in `ARCHITECTURE.md`.                               |
| Time-of-day electricity pricing                           | Roadmap | New per-station price block + one soft term.                                               |
| Driver-shift constraints (must arrive by HH:MM)           | Roadmap | One hard `model.Add(final_arrival[bus] <= deadline)` per bus that has it.                  |
| Multiple routes sharing stations                          | Roadmap | Make `route` a list; the per-station capacity constraint already aggregates across routes. |
| Partial-charge sessions (charge less than full)           | Roadmap | Replace fixed `charge_minutes` with an `IntVar` bounded by `[min, max]`.                   |
| Heterogeneous fleet (per-bus battery range)               | Roadmap | Add `range_km` to `Bus`; the range-cover constraint already iterates per-bus.              |
| Mid-route entry (bus already in transit)                  | Roadmap | Add `start_node` + `start_minute` to `Bus`; geometry adapts.                               |

---

## 6. What the product is _not_

To stay honest about scope, the following are explicitly **out of scope**:

- **Live tracking.** The product is a _planner_, not a real-time fleet monitor. There is no GPS feed, no in-trip rescheduling, no driver app.
- **User accounts.** No authentication, no per-operator dashboards, no role-based access. Anyone with the URL sees everything.
- **Persistent storage.** Schedules are recomputed from the scenario file on every page load. Nothing is written back.
- **Mapping.** No geographic visualisation. The route is treated as a 1-D corridor of segments.
- **Pricing / billing.** The product schedules charging events; it does not bill operators for them.

These are deliberate omissions. They keep the surface small enough that the _scheduling problem itself_ — the part that is genuinely hard — gets the engineering attention it deserves.

---

## 7. How success is measured

For any scheduling run, three numbers tell the story:

| Metric                      | What it measures                                                  | Why it matters                                                         |
| --------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------- |
| **Worst single-bus wait**   | The longest time any one bus spent queued, anywhere, on its trip. | Drivers and passengers experience the worst case, not the average.     |
| **Worst per-operator wait** | The largest sum of waits absorbed by any one operator's fleet.    | Fairness across operators sharing a public charging network.           |
| **Total network wait**      | The sum of all waits across all 20 buses.                         | Aggregate inefficiency — the cost of contention to the system overall. |

Lower is always better. The three weights in the scenario file are the engineer's lever to decide which of the three matters most, today, on this corridor.

---

## 8. Where to go next

- **`README.md`** — how to run the app locally, how to change a weight, how to add a rule.
- **`ARCHITECTURE.md`** — why CP-SAT, how the data model is shaped, what changes were anticipated, the assumptions behind every decision.
- **Hosted app** — <https://jvishvaa.streamlit.app/>.
