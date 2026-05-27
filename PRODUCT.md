# Product

## Who is the user

Fleet operations managers and dispatchers at electric bus operators (KPN, Freshbus, Flixbus). They monitor the network in real time and need to know: at each charging station right now, which bus should charge next and in what order?

Secondary user: network-level planners who adjust scheduling policy (the weights) based on what they learn about real-world fairness and throughput trade-offs.

---

## What the scheduler optimises for

Four objectives, each with a tunable weight:

| Weight                    | What it minimises                                                                 | Why it matters                                                |
| ------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------- |
| `individual`              | Worst single-bus wait at any station                                              | A bus stuck waiting 40 min cascades into the next station too |
| `operator`                | Per-operator total wait (summed separately)                                       | No single operator should consistently bear all the delay     |
| `network`                 | Total wait across all buses and stations                                          | System-wide throughput — every idle minute is wasted capacity |
| `intra_operator_priority` | Cases where a delayed bus waits longer than an on-time bus from the same operator | Within a fleet, the most disrupted buses should recover first |

Hard rules that always hold regardless of weights:

- One bus per charger at a time
- Charging always takes exactly 15 minutes
- Every bus must charge at every station
- No bus starts charging before it physically arrives

---

## Weights comparison: same scenario, different policy

**Scenario 4 — Early arrivals, default weights**
`individual=1.5, operator=1.0, network=0.5, intra_operator_priority=0.8`

| Operator   | Total wait  |
| ---------- | ----------- |
| freshbus   | 122 min     |
| kpn        | 180 min     |
| flixbus    | 15 min      |
| **Spread** | **165 min** |

**Same scenario, operator-fairness priority**
`individual=0.1, operator=5.0, network=0.1, intra_operator_priority=0.1`

| Operator   | Total wait  |
| ---------- | ----------- |
| freshbus   | 140 min     |
| kpn        | 158 min     |
| flixbus    | 15 min      |
| **Spread** | **143 min** |

With high operator weight: KPN's burden drops from 180 → 158, Freshbus increases slightly from 122 → 140. The spread narrows from 165 to 143 minutes — fairer across operators. Total wait drops slightly (317 → 313) as a side effect of the rebalancing.

**Interpretation:** default weights prioritise individual bus efficiency. Heavy operator weight sacrifices some individual efficiency for cross-operator equity.

---

## Assumptions made

**Scenario design**

- Snapshot time 20:45 — mid-evening when all buses are mid-route. Chosen so roughly equal numbers are at each stage of the journey.
- 40 buses per scenario (20 each direction), staggered 12 minutes apart. Staggering is intentional: without it all buses hit every station simultaneously, which is degenerate.
- Operators assigned round-robin (kpn, freshbus, flixbus repeating). This gives each operator ~13 buses and a mix of directions.

**Timing**

- "Earliest arrival" is pre-computed in the scenario file. The solver trusts it as a hard bound — it does not re-derive travel times.
- All times are minutes from snapshot. The loader handles midnight wrap-around with a ±12h disambiguation rule (sufficient for overnight routes).

**Weights**

- Weights are a system-level policy, not per-scenario. The same scheduling principles apply regardless of which snapshot you're looking at.
- The `intra_operator_priority` weight uses wait comparison (not start-time comparison). A delayed bus arriving 3 hours after a normal bus physically cannot charge first — there's nothing to penalise. We only penalise when the scheduler makes a choice that disadvantages the delayed bus.

**Scenario 2 (traffic block)**

- The uniform +30 min delay on the B↔C leg is applied to all buses, which accidentally spreads them further apart. This results in zero wait time — which is correct and reflects a real phenomenon: uniform delays can reduce contention. A more adversarial version would delay only some buses, causing them to collide with on-time buses at B and C.

**Live charges**

- A bus currently charging at snapshot time is modelled as a fixed interval (constants, not variables). The solver cannot reschedule it — the charge is already happening in the physical world.

**What's not modelled**

- Driver breaks, battery state-of-charge, charging speed variation
- Priority passengers or express services
- Multi-depot operations or route deviations
- Bus breakdowns mid-route
