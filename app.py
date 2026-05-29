"""
app.py — Streamlit UI for the Bus Charging Scheduler.

LAYOUT:
  Top:      Scenario dropdown
  Section 1: Scenario data (readable table + raw JSON tabs)
  Section 2: Summary metrics (total wait, objective, operator fairness)
  Section 3: 4 station panels (A, B, C, D) — the charging order

DESIGN DECISIONS:
  - UI does ZERO scheduling logic. It calls solve() and renders.
  - @st.cache_data keyed on scenario file path: re-selecting same scenario
    doesn't re-solve. Changing the scenario reruns the solver.
  - All time display is HH:MM strings (back-converted from minutes offset).
    Internally everything is integers; the UI layer does the conversion.

INTERVIEW HOT SPOTS:
  "Add a new column to the station table" →
    Add a key to the `rows` dict in _render_station(). Done.
  "Show a bar chart of wait times" →
    Use st.bar_chart() in _render_summary() with the operator wait dict.
  "Add a filter: show only buses from operator X" →
    Add a sidebar selectbox for operator, filter the slots before rendering.
  "Total wait time" → already shown via sched.total_wait_min.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

import time
from scheduler.loader import list_scenarios, load_scenario
from scheduler.model import Scenario
from scheduler.solver import ChargingSlot, Schedule, solve

SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ──────────────────────────────────────────────
# Time helpers
# ──────────────────────────────────────────────

def _hhmm(snapshot: str, minutes_offset: int) -> str:
    """Convert snapshot-relative minutes to HH:MM string for display."""
    sh, sm = (int(p) for p in snapshot.split(":"))
    total = sh * 60 + sm + minutes_offset
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


# ──────────────────────────────────────────────
# Cached solver call
# ──────────────────────────────────────────────

@st.cache_data(show_spinner="Solving scheduling problem…")
def _solve_cached(scenario_path: str) -> tuple[Scenario, Schedule, float]:
    """
    Cache keyed on scenario_path string. The solver only runs when a new
    scenario is selected. Streamlit reruns this function if the cache key
    changes (i.e. different scenario picked).
    Returns (scenario, schedule, solve_time_seconds).
    """
    s = load_scenario(scenario_path)
    t0 = time.time()
    sched = solve(s, time_limit_s=20.0)
    solve_time = round(time.time() - t0, 2)
    return s, sched, solve_time


# ──────────────────────────────────────────────
# Render helpers
# ──────────────────────────────────────────────

def _render_scenario_view(scenario: Scenario) -> None:
    """
    Show the scenario data in two tabs: a readable table and raw JSON.
    This is what the reviewer uses to verify the scenario makes sense.
    """
    tab_table, tab_json = st.tabs(["📋 Readable table", "{ } Raw JSON"])

    with tab_table:
        st.write(
            f"**{scenario.name}**  ·  snapshot **{scenario.snapshot_time}**  ·  "
            f"charge **{scenario.charge_minutes} min**  ·  travel/leg **{scenario.travel_minutes_per_leg} min**"
        )

        # Summary metrics
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total buses", len(scenario.buses))
        c2.metric("A→D buses", sum(1 for b in scenario.buses if b.direction == "A->D"))
        c3.metric("D→A buses", sum(1 for b in scenario.buses if b.direction == "D->A"))
        c4.metric("Operators", len({b.operator for b in scenario.buses}))
        c5.metric("Stations", len(scenario.stations))

        # Operator-wise fleet breakdown
        from collections import Counter
        op_counts = Counter(b.operator for b in scenario.buses)
        op_status_counts: dict = {}
        for b in scenario.buses:
            op_status_counts.setdefault(b.operator, Counter())[b.status] += 1

        st.markdown("**Fleet by operator**")
        op_cols = st.columns(len(op_counts))
        for col, (op, total) in zip(op_cols, sorted(op_counts.items())):
            sc = op_status_counts[op]
            breakdown = "  ·  ".join(
                f"{count} {status}"
                for status, count in sorted(sc.items())
            )
            col.metric(op.upper(), f"{total} buses", breakdown)

        # Weights (global)
        st.caption(
            f"**Weights (from scenario file)** — "
            f"individual: `{scenario.weights.individual}` · "
            f"operator: `{scenario.weights.operator}` · "
            f"network: `{scenario.weights.network}` · "
            f"intra-operator priority: `{scenario.weights.intra_operator_priority}`"
        )

        # Station configs
        st.caption("**Station charger counts:** " + "  ·  ".join(
            f"{name}: {cfg.chargers} chargers"
            for name, cfg in scenario.stations.items()
        ))

        # Bus table
        rows = []
        for b in scenario.buses:
            upcoming_str = " → ".join(
                f"{u.station}@{_hhmm(scenario.snapshot_time, u.actual_arrival_min)}"
                + (
                    f" (sched {_hhmm(scenario.snapshot_time, u.scheduled_arrival_min)})"
                    if u.delay_min != 0 else ""
                )
                for u in b.upcoming
            )
            rows.append({
                "bus": b.id,
                "operator": b.operator,
                "direction": b.direction,
                "status": b.status,
                "where": b.location_desc,
                "upcoming stops": upcoming_str,
            })
        st.dataframe(rows, hide_index=True, use_container_width=True)

    with tab_json:
        st.json(scenario.raw)


def _render_summary(scenario: Scenario, sched: Schedule, solve_time: float) -> None:
    """
    High-level metrics about the solved schedule.
    INTERVIEW: "Show total wait per operator" → this is it.
    """
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Solver status", sched.status)
    c2.metric("Total wait (all buses)", f"{sched.total_wait_min} min")
    c3.metric("Avg wait per bus", f"{sched.total_wait_min / max(len(scenario.buses), 1):.1f} min")
    c4.metric("Objective value", f"{sched.objective:,.0f}")
    c5.metric("Solve time", f"{solve_time}s", help="Cached after first run — re-selecting same scenario shows 0s")

    # Per-operator wait breakdown
    op_waits = sched.total_wait_by_operator()
    if op_waits:
        st.caption("**Wait time by operator:**  " + "  ·  ".join(
            f"{op}: **{wait} min**" for op, wait in sorted(op_waits.items())
        ))


def _render_station(station: str, scenario: Scenario, sched: Schedule) -> None:
    """
    Show charging order at one station.
    Columns: rank, bus ID, operator, direction, arrival (HH:MM),
             charge start (HH:MM), charge end (HH:MM), curr wait, total wait, delay.

    INTERVIEW: "Add a column for cumulative wait" → add "total_wait" key to rows.
    INTERVIEW: "Highlight buses that waited > 15 min" → use st.dataframe with
               a Styler to colour rows conditionally.
    """
    chargers = scenario.stations[station].chargers
    order = sched.order_at(station)

    # Compute station-level wait total
    station_wait = sum(slot.curr_wait_min for slot in order)

    st.subheader(f"Station {station}")
    st.caption(f"{chargers} charger(s)  ·  {len(order)} charges  ·  wait at this station: {station_wait} min")

    if not order:
        st.info("No buses scheduled at this station.")
        return

    rows = []
    for i, slot in enumerate(order, 1):
        delay_label = (
            f"+{slot.delay_min} min late" if slot.delay_min > 0
            else (f"{slot.delay_min} min early" if slot.delay_min < 0 else "on time")
        )
        rows.append({
            "#":            i,
            "bus":          slot.bus_id,
            "operator":     slot.operator,
            "direction":    slot.direction,
            "arrives":      _hhmm(scenario.snapshot_time, slot.actual_arrival_min),
            "sched arrival":_hhmm(scenario.snapshot_time, slot.scheduled_arrival_min),
            "charge start": _hhmm(scenario.snapshot_time, slot.start_min),
            "charge end":   _hhmm(scenario.snapshot_time, slot.end_min),
            "curr wait":    f"{slot.curr_wait_min} min",
            "total wait":   f"{slot.total_wait_min} min",
            "status":       delay_label,
        })

    st.dataframe(rows, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main() -> None:
    st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
    st.title("🚌 Bus Charging Scheduler")

    paths = list_scenarios(SCENARIOS_DIR)
    if not paths:
        st.error(f"No scenario files found in {SCENARIOS_DIR}.")
        return

    name_to_path = {p.stem: p for p in paths}
    pick = st.selectbox(
        "Select scenario",
        list(name_to_path.keys()),
        format_func=lambda s: s.replace("_", " ").replace("scenario ", "Scenario ").title(),
    )

    scenario, sched, solve_time = _solve_cached(str(name_to_path[pick]))

    st.divider()
    st.markdown("### 📂 Scenario data")
    _render_scenario_view(scenario)

    st.divider()
    st.markdown("### 📊 Schedule summary")
    _render_summary(scenario, sched, solve_time)

    st.divider()
    st.markdown("### ⚡ Charging order by station")
    cols = st.columns(4)
    for col, station in zip(cols, ["A", "B", "C", "D"]):
        with col:
            _render_station(station, scenario, sched)


if __name__ == "__main__":
    main()