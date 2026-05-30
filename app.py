"""
Streamlit UI — pick a scenario, see the input, see the schedule.

Three views as required by the assessment doc:
  1. Scenario data (raw input + readable table)
  2. Per-bus timetable (full timeline for each bus)
  3. Per-station charging order (who charged at A/B/C/D and in what order)

Zero scheduling logic lives here — everything goes through `solve(scenario)`.
"""
from __future__ import annotations

import time
from pathlib import Path

import streamlit as st

from scheduler.loader import list_scenarios, load_scenario
from scheduler.model import Scenario
from scheduler.solver import Schedule, solve


SCENARIOS_DIR = Path(__file__).parent / "scenarios"


# ──────────────────────────────────────────────
# Time helpers — solver works in minutes-from-reference; UI shows HH:MM.
# ──────────────────────────────────────────────
def _hhmm(reference: str, minutes_offset: int) -> str:
    rh, rm = (int(p) for p in reference.split(":"))
    total = rh * 60 + rm + minutes_offset
    total %= 24 * 60
    return f"{total // 60:02d}:{total % 60:02d}"


# ──────────────────────────────────────────────
# Solver call (cached on file path so re-selecting the same scenario is instant)
# ──────────────────────────────────────────────
@st.cache_data(show_spinner="Solving scheduling problem…")
def _solve_cached(scenario_path: str) -> tuple[Scenario, Schedule, float]:
    s = load_scenario(scenario_path)
    t0 = time.time()
    sched = solve(s, time_limit_s=30.0)
    return s, sched, round(time.time() - t0, 2)


# ──────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────
def _render_scenario_view(scenario: Scenario) -> None:
    tab_table, tab_json = st.tabs(["Readable table", "Raw JSON"])

    with tab_table:
        forward_dir = f"{scenario.route.endpoints[0]}->{scenario.route.endpoints[1]}"
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total buses", len(scenario.buses))
        c2.metric("Forward direction",
                  sum(1 for b in scenario.buses if b.direction == forward_dir))
        c3.metric("Reverse direction",
                  sum(1 for b in scenario.buses if b.direction != forward_dir))
        c4.metric("Operators", len({b.operator for b in scenario.buses}))
        c5.metric("Stations", len(scenario.stations))

        st.caption(
            f"**Reference time:** `{scenario.reference_time}` "
            f"· **Charge:** {scenario.charge_minutes} min "
            f"· **Range:** {scenario.battery_range_km} km "
            f"· **Speed:** {scenario.speed_kmph} km/h"
        )

        st.caption(
            "**Route:** "
            + " → ".join(scenario.route.nodes)
            + "  ·  segments (km): "
            + ", ".join(f"{s.from_node}→{s.to_node} {s.distance_km}"
                        for s in scenario.route.segments)
        )

        st.caption(
            "**Stations / chargers:** "
            + "  ·  ".join(f"{n}: {cfg.chargers}"
                          for n, cfg in scenario.stations.items())
        )

        st.caption(
            f"**Weights** — individual: `{scenario.weights.individual}`  "
            f"·  operator: `{scenario.weights.operator}`  "
            f"·  overall: `{scenario.weights.overall}`"
        )

        rows = [
            {
                "bus": b.id,
                "operator": b.operator,
                "direction": b.direction,
                "departure": _hhmm(scenario.reference_time, b.departure_min),
            }
            for b in scenario.buses
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)

    with tab_json:
        st.json(scenario.raw)


def _render_summary(scenario: Scenario, sched: Schedule, solve_time: float) -> None:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Solver status", sched.status)
    c2.metric("Total wait", f"{sched.total_wait_min} min")
    c3.metric("Avg wait / bus",
              f"{sched.total_wait_min / max(len(scenario.buses), 1):.1f} min")
    c4.metric("Objective", f"{sched.objective:,.0f}")
    c5.metric("Solve time", f"{solve_time}s",
              help="Cached after first run — re-selecting the same scenario shows ~0s")

    op_waits = sched.total_wait_by_operator()
    if op_waits:
        st.caption(
            "**Wait by operator:**  "
            + "  ·  ".join(f"{op}: **{w} min**"
                          for op, w in sorted(op_waits.items()))
        )


def _render_bus_timetable(scenario: Scenario, sched: Schedule) -> None:
    """Per-bus timeline: departure → each charge (station, arrive, wait, end) → arrival."""
    rows = []
    for b in scenario.buses:
        tl = sched.by_bus.get(b.id)
        if tl is None:
            rows.append({
                "bus": b.id,
                "operator": b.operator,
                "direction": b.direction,
                "departure": _hhmm(scenario.reference_time, b.departure_min),
                "stations": "—",
                "schedule": "no feasible plan",
                "total wait": "—",
                "arrival": "—",
            })
            continue

        if tl.charges:
            schedule_str = "  →  ".join(
                f"{c.station} arr {_hhmm(scenario.reference_time, c.arrive_min)}"
                f" / charge {_hhmm(scenario.reference_time, c.start_min)}–"
                f"{_hhmm(scenario.reference_time, c.end_min)}"
                + (f" (wait {c.wait_min}m)" if c.wait_min > 0 else "")
                for c in tl.charges
            )
            stations_str = " → ".join(c.station for c in tl.charges)
        else:
            schedule_str = "no charging needed"
            stations_str = "—"

        rows.append({
            "bus": tl.bus_id,
            "operator": tl.operator,
            "direction": tl.direction,
            "departure": _hhmm(scenario.reference_time, tl.departure_min),
            "stations": stations_str,
            "schedule": schedule_str,
            "total wait": f"{tl.total_wait_min} min",
            "arrival": _hhmm(scenario.reference_time, tl.arrival_at_destination_min),
        })

    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_station(station: str, scenario: Scenario, sched: Schedule) -> None:
    cfg = scenario.stations[station]
    order = sched.order_at(station)

    st.subheader(f"Station {station}")
    st.caption(
        f"{cfg.chargers} charger(s)  ·  {len(order)} charges  ·  "
        f"total wait at station: {sum(e.wait_min for e in order)} min"
    )

    if not order:
        st.info("No buses charged at this station.")
        return

    rows = []
    for i, e in enumerate(order, 1):
        rows.append({
            "#": i,
            "bus": e.bus_id,
            "operator": e.operator,
            "direction": e.direction,
            "arrives": _hhmm(scenario.reference_time, e.arrive_min),
            "charge start": _hhmm(scenario.reference_time, e.start_min),
            "charge end": _hhmm(scenario.reference_time, e.end_min),
            "wait": f"{e.wait_min} min",
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
    st.title("Bus Charging Scheduler")

    paths = list_scenarios(SCENARIOS_DIR)
    if not paths:
        st.error(f"No scenario files found in {SCENARIOS_DIR}.")
        return

    name_to_path = {p.stem: p for p in paths}
    pick = st.selectbox(
        "Select scenario",
        list(name_to_path.keys()),
        format_func=lambda s: s.replace("_", " ").title(),
    )

    scenario, sched, solve_time = _solve_cached(str(name_to_path[pick]))

    st.divider()
    st.markdown("### Scenario data")
    _render_scenario_view(scenario)

    st.divider()
    st.markdown("### Schedule summary")
    _render_summary(scenario, sched, solve_time)

    st.divider()
    st.markdown("### Per-bus timetable")
    _render_bus_timetable(scenario, sched)

    st.divider()
    st.markdown("### Charging order by station")
    station_names = list(scenario.stations.keys())
    cols = st.columns(max(len(station_names), 1))
    for col, station in zip(cols, station_names):
        with col:
            _render_station(station, scenario, sched)


if __name__ == "__main__":
    main()
