"""
Streamlit UI — Core Optimization Dashboard

Three views as required by the assessment doc:
  1. Scenario data (raw input + readable table)
  2. Per-Fleet Timetable View (full timeline for each bus)
  3. Per-station charging order (who charged at A/B/C/D and in what order)

Zero scheduling logic lives here — everything goes through `solve(scenario)`.
"""

from __future__ import annotations

import time
from pathlib import Path
import pandas as pd
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
@st.cache_data(show_spinner="Calculating optimal schedule...")
def _solve_cached(scenario_path: str) -> tuple[Scenario, Schedule, float]:
    s = load_scenario(scenario_path)
    t0 = time.time()
    sched = solve(s, time_limit_s=30.0)
    return s, sched, round(time.time() - t0, 2)


# ──────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────
def _render_scenario_view(scenario: Scenario) -> None:
    tab_table, tab_json = st.tabs(["Scenario Overview", "Raw Data (JSON)"])

    with tab_table:
        forward_dir = f"{scenario.route.endpoints[0]}->{scenario.route.endpoints[1]}"
        
        # Summary metric cards
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Buses", len(scenario.buses))
        c2.metric("Bengaluru → Kochi", sum(1 for b in scenario.buses if b.direction == forward_dir))
        c3.metric("Kochi → Bengaluru", sum(1 for b in scenario.buses if b.direction != forward_dir))
        c4.metric("Unique Operators", len({b.operator for b in scenario.buses}))
        c5.metric("Charging Stations", len(scenario.stations))
        
        # Metadata settings
        st.markdown("##### Route & Charging Settings")
        meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
        meta_col1.caption(f"**Reference Time:** {scenario.reference_time}")
        meta_col2.caption(f"**Charge Duration:** {scenario.charge_minutes} min")
        meta_col3.caption(f"**Max Battery Range:** {scenario.battery_range_km} km")
        meta_col4.caption(f"**Average Speed:** {scenario.speed_kmph} km/h")

        st.markdown("---")
        st.markdown("##### Route Distances")
        segments_data = [
            {"Route Segment": f"{s.from_node} ⇄ {s.to_node}", "Distance (km)": s.distance_km}
            for s in scenario.route.segments
        ]
        st.dataframe(segments_data, hide_index=True, use_container_width=True)

        st.markdown("---")
        st.markdown("##### Optimization Goal Weights")
        w_col1, w_col2, w_col3 = st.columns(3)
        w_col1.caption(f"**Individual Bus Wait Weight:** {scenario.weights.individual}")
        w_col2.caption(f"**Operator Fairness Weight:** {scenario.weights.operator}")
        w_col3.caption(f"**Overall Network Wait Weight:** {scenario.weights.overall}")

        st.markdown("---")
        st.markdown("##### Planned Departures")
        rows = [
            {
                "Bus ID": b.id,
                "Operator": b.operator.upper(),
                "Route Direction": b.direction,
                "Departure Time": _hhmm(scenario.reference_time, b.departure_min),
            }
            for b in scenario.buses
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)

    with tab_json:
        st.json(scenario.raw)


def _render_summary(scenario: Scenario, sched: Schedule, solve_time: float) -> None:
    st.markdown("#### Schedule Metrics")
    
    # Solver status notification
    if sched.status == "OPTIMAL":
        st.success("Optimal schedule found successfully.")
    elif sched.status == "FEASIBLE":
        st.info("Feasible schedule found.")
    else:
        st.error(f"Solver ended with status: {sched.status}")

    # Standard summary metrics cards
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Wait Time", f"{sched.total_wait_min} min")
    m2.metric("Average Wait per Bus", f"{sched.total_wait_min / max(len(scenario.buses), 1):.1f} min")
    m3.metric("Objective Score", f"{sched.objective:,.0f}", help="The combined cost score minimized by the solver.")
    m4.metric("Solver Runtime", f"{solve_time}s")
    m5.metric("Total Charges", str(sched.total_charge_min // scenario.charge_minutes))


def _render_bus_timetable(scenario: Scenario, sched: Schedule) -> None:
    """Per-bus timeline: departure → each charge (station, arrive, wait, end) → arrival."""
    rows = []
    for b in scenario.buses:
        tl = sched.by_bus.get(b.id)
        if tl is None:
            rows.append({
                "Bus ID": b.id,
                "Operator": b.operator.upper(),
                "Route Direction": b.direction,
                "Departure Time": _hhmm(scenario.reference_time, b.departure_min),
                "Stations Used": "Bypassed",
                "Detailed Charging Timeline": "No valid schedule generated.",
                "Total Wait Time": "—",
                "Arrival Time": "—",
            })
            continue

        if tl.charges:
            schedule_str = "  →  ".join(
                f"[{c.station}] Arrived {_hhmm(scenario.reference_time, c.arrive_min)} | "
                f"Charged: {_hhmm(scenario.reference_time, c.start_min)}–{_hhmm(scenario.reference_time, c.end_min)}"
                + (f" (Waited: {c.wait_min}m)" if c.wait_min > 0 else " (No Wait)")
                for c in tl.charges
            )
            stations_str = " → ".join(c.station for c in tl.charges)
        else:
            schedule_str = "Direct Trip — No charging required"
            stations_str = "None"

        rows.append({
            "Bus ID": tl.bus_id,
            "Operator": tl.operator.upper(),
            "Route Direction": tl.direction,
            "Departure Time": _hhmm(scenario.reference_time, tl.departure_min),
            "Stations Used": stations_str,
            "Detailed Charging Timeline": schedule_str,
            "Total Wait Time": f"{tl.total_wait_min} min",
            "Arrival Time": _hhmm(scenario.reference_time, tl.arrival_at_destination_min),
        })

    st.dataframe(rows, hide_index=True)


def _render_station(station: str, scenario: Scenario, sched: Schedule) -> None:
    cfg = scenario.stations[station]
    order = sched.order_at(station)

    # Informational subtitle bar
    st.caption(
        f"Available Chargers: {cfg.chargers} | "
        f"Total Charges: {len(order)} | "
        f"Total Station Wait: {sum(e.wait_min for e in order)} min"
    )

    if not order:
        st.info("No buses are scheduled to charge at this station.")
        return

    rows = []
    for i, e in enumerate(order, 1):
        rows.append({
            "Queue Order": f"#{i:02d}",
            "Bus ID": e.bus_id,
            "Operator": e.operator.upper(),
            "Route Direction": e.direction,
            "Arrival Time": _hhmm(scenario.reference_time, e.arrive_min),
            "Charge Start": _hhmm(scenario.reference_time, e.start_min),
            "Charge End": _hhmm(scenario.reference_time, e.end_min),
            "Wait Time": f"{e.wait_min} min" if e.wait_min > 0 else "0 min (No Wait)"
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# Main Application Environment
# ──────────────────────────────────────────────
def main() -> None:
    st.set_page_config(
        page_title="Electric Bus Charging Scheduler",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Clean CSS modifications for styling
    st.markdown("""
        <style>
        .stApp {
            background-color: #F8FAFC;
            color: #0F172A;
        }
        .block-container {
            max-width: 1440px;
            padding-top: 2rem;
            padding-bottom: 2rem;
        }
        section[data-testid="stSidebar"] {
            background-color: #F8AB00 !important;
        }
        section[data-testid="stSidebar"] h3, section[data-testid="stSidebar"] p {
            color: #000000 !important;
        }
        div[data-testid="stMetric"] {
            background-color: #FFFFFF;
            border: 1px solid #E2E8F0;
            border-radius: 12px;
            padding: 16px;
            box-shadow: 0 1px 3px 0 rgba(0, 0, 0, 0.05);
        }
        div[data-testid="stMetricLabel"] > div {
            color: #64748B !important;
            font-size: 0.85rem !important;
            text-transform: uppercase;
            font-weight: 600;
            letter-spacing: 0.05em;
        }
        div[data-testid="stMetricValue"] > div {
            color: #0F172A !important;
            font-size: 1.75rem !important;
            font-weight: 700;
        }
        .stTabs [data-baseweb="tab"] {
            font-weight: 600;
            color: #64748B;
        }
        .stTabs [aria-selected="true"] {
            color: #2563EB !important;
            border-bottom-color: #2563EB !important;
        }
        </style>
        """, unsafe_allow_html=True)
    
    # Dashboard Header
    st.title("Electric Bus Charging Scheduler")
    st.caption("Centralized Multi-Operator Constraint Optimization Platform | Powered by Google OR-Tools CP-SAT")
    st.markdown("---")

    paths = list_scenarios(SCENARIOS_DIR)
    if not paths:
        st.error(f"Error: No scenario files found in directory: {SCENARIOS_DIR}.")
        return

    name_to_path = {p.stem: p for p in paths}
    
    # Control Panel Layout Partitioning
    with st.sidebar:
        st.markdown("### Control Panel")
        # st.caption("Select a scenario to run the scheduler.")
        
        pick = st.selectbox(
            "Select Scenario to run the scheduler.",
            list(name_to_path.keys()),
            format_func=lambda s: s.replace("_", " ").title(),
        )
        
        st.markdown("---")
        st.markdown("##### Route Information")
        st.caption("**Route Type:** Bi-directional Corridor")
        st.caption("**Route Ends:** Bengaluru ⇄ Kochi")

    # Pipeline Computation Triggers
    scenario, sched, solve_time = _solve_cached(str(name_to_path[pick]))
    
    # Navigation views tabs
    view_selector = st.radio(
        "Select Dashboard View",
        ["System Performance Analytics", "Fleet Timetable View", "Station Queues"],
        horizontal=True
    )
    st.markdown("---")

    if view_selector == "System Performance Analytics":
        _render_summary(scenario, sched, solve_time)
        
        st.markdown("---")
        st.markdown("#### Operator Performance Matrices")
        op_rows = [
            {"Operator": op.upper(), "Total Wait Time": f"{wait} min"}
            for op, wait in sched.total_wait_by_operator().items()
        ]
        st.dataframe(op_rows, hide_index=True, use_container_width=True)

        st.markdown("---")
        st.markdown("#### Source Scenario Parameter Profiler")
        _render_scenario_view(scenario)

    elif view_selector == "Fleet Timetable View":
        st.markdown("#### Bus Master Timetable")
        st.caption("Complete trip timeline for each bus, including departure, wait times, charging sessions, and arrival.")
        _render_bus_timetable(scenario, sched)

    elif view_selector == "Station Queues":
        st.markdown("#### Physical Station Queue Tracking Tables")
        st.caption("Detailed order and timing of buses charging at each station.")
        
        station_names = list(scenario.stations.keys())
        
        # Station load summaries
        summary_cols = st.columns(len(station_names))
        for col, station in zip(summary_cols, station_names):
            with col:
                events = sched.order_at(station)
                st.metric(
                    label=f"Station {station} Load", 
                    value=f"{len(events)} Charges", 
                    help=f"Total number of times a bus charges at station {station}."
                )
        
        st.markdown("---")
        
        # Station timelines dropdown accordions
        for station in station_names:
            with st.expander(f"🔌 Station {station} Queue Details", expanded=False):
                _render_station(station, scenario, sched)


if __name__ == "__main__":
    main()