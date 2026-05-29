# Product

## What this is

A real-time charging scheduler for electric bus fleets. Given a snapshot of the network — where every bus is right now, what it's doing, where it's heading — it produces the optimal charging order at each station.

The output is simple: at station A, charge bus X first, then Y, then Z. At station B, charge P first, then Q. And so on for all four stations. The scheduler does the work so the dispatcher doesn't have to think about it.

---

## Who uses it

**Primary user — Network planners** who set scheduling policy. They adjust the weights to define what "good" means for their fleet — is it more important to protect a single stranded bus, or to keep the whole fleet moving efficiently?

**Secondary user — Fleet dispatchers and operations managers** at electric bus operators (KPN, Freshbus, Flixbus). They watch the network in real time and need to know: given what's happening right now, who charges next at each station? Without a scheduler they'd have to make this call manually across 40 buses and 4 stations simultaneously.

---

## What the scheduler optimises for

The scheduler minimises a weighted combination of four things:

### Individual fairness (`individual`, weight 1.5)

**What:** the worst single-bus wait across all buses and all stations — measured as max, not sum.

**Why:** a bus stuck waiting 38 minutes at station B will be late to station C too, cascading through the rest of its route. The max metric means the solver specifically targets the worst outlier, not just average wait.

**Effect of increasing this weight:** the solver works harder to eliminate the single worst wait, even if it slightly increases total wait elsewhere.

### Operator fairness (`operator`, weight 1.0)

**What:** total wait accumulated by each operator's fleet, tracked separately per operator.

**Why:** if one operator's buses consistently get pushed to the back of the queue, that operator is subsidising the schedule efficiency of others. This term ensures no operator is systematically disadvantaged.

**Effect of increasing this weight:** wait time is distributed more evenly across operators. May slightly increase total system wait in exchange for better cross-operator equity.

### Network throughput (`network`, weight 0.5)

**What:** sum of all wait times across all buses and all stations — the total idle time in the system.

**Why:** every minute a bus sits waiting at a charger is a minute it's not available for its next leg. This term keeps the whole fleet moving efficiently.

**Effect of increasing this weight:** the solver tries harder to reduce overall idle time across all buses, even if this means some individual buses wait longer.

### Intra-operator priority (`intra_operator_priority`, weight 0.8)

**What:** within the same operator's fleet at the same station, a more-delayed bus should not wait longer than a less-delayed one.

**Why:** if bus A is running 30 minutes late and bus B is on time, and they both arrive at station C at the same moment, bus A should charge first — it has more ground to recover. This term enforces that priority.

**Important:** this compares wait times (start − actual_arrival), not start times. A delayed bus that arrives 3 hours after an on-time bus will naturally start later — that's physics, not unfairness. We only penalise when the scheduler actively makes the delayed bus queue longer.

**Effect of increasing this weight:** stronger enforcement of delay-based priority within each operator's fleet.

---

## Hard rules (always enforced, regardless of weights)

| Rule                                                | Why it's hard, not soft                             |
| --------------------------------------------------- | --------------------------------------------------- |
| One bus per charger at a time                       | Physical constraint — you can't share a charger     |
| Charging always takes exactly 15 minutes            | Fixed hardware — no partial charges                 |
| Every bus must charge at every station              | Operational requirement — battery capacity          |
| No bus starts charging before it physically arrives | Causality — can't schedule what hasn't happened yet |

---

## The 5 scenarios

Each scenario is a JSON file describing the network at a single frozen moment (20:45). The scheduler reads the file and produces a schedule — no assumptions about what "normal" looks like.

### Scenario 1 — Everything on schedule

Every bus is exactly where it should be. No delays, no surprises. Produces a clean, predictable charging order — buses charge in roughly arrival order with minimal waiting.

**Total wait: 16 min across all buses. Max single wait: 3 min.**

### Scenario 2 — Traffic block between B and C (gradual clearing)

A genuine traffic disruption on the B↔C leg, gradually clearing after 22:00. Buses crossing B↔C near snapshot time (20:45) face up to 30 minutes extra delay (±20% noise). Buses crossing after 22:00 travel freely.

This creates a wave of delayed buses that catches up with on-time buses at stations B and C — real contention that the scheduler has to resolve.

**Total wait: 79 min. Max single wait: 8 min.**

_Note on gradual vs uniform delay: a uniform +30 min on all buses would actually spread them further apart and eliminate contention. Gradual clearing is the realistic model — only early buses are affected._

### Scenario 3 — Late starters clashing

14 of 40 buses started their route 25–40 minutes late (maintenance, driver issues, depot delays). They've now caught up with the buses immediately behind them on schedule, causing unexpected contention at stations that were supposed to be quiet.

**Total wait: 170 min. Max single wait: 9 min.**

### Scenario 4 — Early arrivals

10 buses are running 10–15 minutes ahead per leg — light traffic, fast driving. They arrive at stations before the buses that were supposed to charge first, jumping the queue.

**Total wait: 313 min. Max single wait: 38 min.**

### Scenario 5 — Mixed reality

A realistic evening: some buses on time, some late, some early, no single dominant pattern. This is what the network actually looks like on a normal night.

**Total wait: 262 min. Max single wait: 38 min.**

---

## Weight comparison: same scenario, different policy

Using **Scenario 5 (Mixed reality)** to show what changes when you adjust the `individual` weight:

**Default weights** (`individual=1.5, operator=1.0, network=0.5, intra_operator_priority=0.8`):

| Operator  | Total wait  | Max single wait |
| --------- | ----------- | --------------- |
| flixbus   | 159 min     | —               |
| kpn       | 92 min      | —               |
| freshbus  | 11 min      | —               |
| **Total** | **262 min** | **38 min**      |

**High individual weight** (`individual=5.0, operator=0.1, network=0.1, intra_operator_priority=0.1`):

| Operator  | Total wait  | Max single wait |
| --------- | ----------- | --------------- |
| flixbus   | 146 min     | —               |
| kpn       | 116 min     | —               |
| freshbus  | 14 min      | —               |
| **Total** | **276 min** | **31 min**      |

**What changed:** with a high individual weight, the solver specifically targets the worst-waiting buses. The max single wait drops from 38 → 31 minutes. Total wait increases slightly (262 → 276) because optimising for the worst case sometimes means letting the average slip.

**Interpretation:** default weights balance individual fairness with overall throughput. A high individual weight is the right policy when stranded buses cause cascading delays — you pay a small total-wait price to prevent the worst case.

---

## Assumptions and design decisions

### Snapshot timing

The snapshot is taken at **20:45** — mid-evening, when most buses are mid-route and a natural mix of statuses (charging, traveling, waiting) is visible. Early morning or midday would have too few buses in transit.

### 40 buses, staggered 12 minutes apart

Without staggering, all buses hit the same station simultaneously — a degenerate problem with no interesting scheduling decisions. 12-minute stagger with ±6 minute jitter creates realistic overlap without overwhelming the chargers.

### Operators assigned round-robin

KPN, Freshbus, Flixbus cycle through buses 0–39. This gives each operator ~13 buses with a natural mix of directions. No operator is structurally advantaged by having all buses going the same direction.

### `actual_arrival` is a hard bound

The solver trusts `actual_arrival` from the JSON as the physical lower bound. It does not re-derive travel times. This means the scenario file is the single source of truth — if you want to model a bus arriving early, change its `actual_arrival` in the JSON.

### Weights are per-scenario via the JSON

Each scenario file carries its own `"weights"` block. The loader reads them at runtime.

### What's not modelled

- **Battery state of charge** — assumed buses always need a full 15-minute charge
- **Driver breaks and shift limits** — buses are always available
- **Charging speed variation** — all chargers are identical
- **Multi-depot operations** — buses always start and end at the route endpoints
- **Bus breakdowns mid-route** — once a bus is on the route it completes it
- **Priority passengers or express services** — all buses are equal weight
