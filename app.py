"""
Streamlit UI — Core Optimization Dashboard

Three views as required by the assessment doc:
  1. Scenario data (raw input + readable table)
  2. Per-bus timetable (full timeline for each bus)
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
@st.cache_data(show_spinner="Executing Mixed-Integer CP-SAT Optimization Matrix...")
def _solve_cached(scenario_path: str) -> tuple[Scenario, Schedule, float]:
    s = load_scenario(scenario_path)
    t0 = time.time()
    sched = solve(s, time_limit_s=30.0)
    return s, sched, round(time.time() - t0, 2)


# ──────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────
def _render_scenario_view(scenario: Scenario) -> None:
    tab_table, tab_json = st.tabs(["Structured Operational Manifest", "Raw Configuration JSON"])

    with tab_table:
        forward_dir = f"{scenario.route.endpoints[0]}->{scenario.route.endpoints[1]}"
        
        # High-density summary cards
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total Active Fleet", len(scenario.buses))
        c2.metric("Bengaluru → Kochi", sum(1 for b in scenario.buses if b.direction == forward_dir))
        c3.metric("Kochi → Bengaluru", sum(1 for b in scenario.buses if b.direction != forward_dir))
        c4.metric("Unique Operators", len({b.operator for b in scenario.buses}))
        c5.metric("Network Nodes", len(scenario.stations))
        
        # Clean metadata specifications
        st.markdown("##### System Infrastructure Parameters")
        meta_col1, meta_col2, meta_col3, meta_col4 = st.columns(4)
        meta_col1.caption(f"**Reference Epoch:** {scenario.reference_time}")
        meta_col2.caption(f"**Charge Duration:** {scenario.charge_minutes} min")
        meta_col3.caption(f"**Battery Range Window:** {scenario.battery_range_km} km")
        meta_col4.caption(f"**Nominal Velocity:** {scenario.speed_kmph} km/h")

        st.markdown("---")
        st.markdown("##### Segment Topology & Network Proximity")
        segments_data = [
            {"Segment Link": f"{s.from_node} ⇄ {s.to_node}", "Distance (km)": s.distance_km}
            for s in scenario.route.segments
        ]
        # Removed use_container_width here to prevent tab layout loops
        st.dataframe(segments_data, hide_index=True)

        st.markdown("---")
        st.markdown("##### Base Priority Weights")
        w_col1, w_col2, w_col3 = st.columns(3)
        w_col1.caption(f"**Individual Delay Target:** {scenario.weights.individual}")
        w_col2.caption(f"**Operator Equity Target:** {scenario.weights.operator}")
        w_col3.caption(f"**System Throughput Target:** {scenario.weights.overall}")

        st.markdown("---")
        st.markdown("##### Raw Fleet Departure Queue")
        rows = [
            {
                "Bus Identifier": b.id,
                "Operator Fleet": b.operator.upper(),
                "Direction Bounds": b.direction,
                "Scheduled Departure": _hhmm(scenario.reference_time, b.departure_min),
            }
            for b in scenario.buses
        ]
        # Removed use_container_width here to avoid inner tab sizing cycles
        st.dataframe(rows, hide_index=True)

    with tab_json:
        st.json(scenario.raw)


def _render_summary(scenario: Scenario, sched: Schedule, solve_time: float) -> None:
    st.markdown("#### Operational Core Telemetry")
    
    # Semantic execution flags
    if sched.status == "OPTIMAL":
        st.success("Mathematical Optimality Proven (Global Penalty Minimized)")
    elif sched.status == "FEASIBLE":
        st.info("Feasible Operational Schedule Formulated")
    else:
        st.error(f"Solver Terminated with Status Flags: {sched.status}")

    # Native corporate-grade metric cards
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Cumulative Wait Time", f"{sched.total_wait_min} min")
    m2.metric("Mean Wait / Vehicle", f"{sched.total_wait_min / max(len(scenario.buses), 1):.1f} min")
    m3.metric("System Stress Index", f"{sched.objective:,.0f}", help="Scaled multi-term objective penalty score.")
    m4.metric("Matrix Solver Runtime", f"{solve_time}s")
    m5.metric("Total Charging Events", str(sched.total_charge_min // scenario.charge_minutes))


def _render_bus_timetable(scenario: Scenario, sched: Schedule) -> None:
    """Per-bus timeline: departure → each charge (station, arrive, wait, end) → arrival."""
    rows = []
    for b in scenario.buses:
        tl = sched.by_bus.get(b.id)
        if tl is None:
            rows.append({
                "Bus Identifier": b.id,
                "Operator": b.operator.upper(),
                "Direction Bounds": b.direction,
                "Departure": _hhmm(scenario.reference_time, b.departure_min),
                "Charging Sequence": "Bypassed",
                "Full Analytical Timeline": "No feasible tracking profile mapped.",
                "Total Delay": "—",
                "Terminal Arrival": "—",
            })
            continue

        if tl.charges:
            schedule_str = "  →  ".join(
                f"[{c.station}] Arr {_hhmm(scenario.reference_time, c.arrive_min)} | "
                f"Session: {_hhmm(scenario.reference_time, c.start_min)}–{_hhmm(scenario.reference_time, c.end_min)}"
                + (f" (Wait: {c.wait_min}m)" if c.wait_min > 0 else " (No Wait)")
                for c in tl.charges
            )
            stations_str = " → ".join(c.station for c in tl.charges)
        else:
            schedule_str = "Direct Transit — No Intermediate Charging Triggered"
            stations_str = "None"

        rows.append({
            "Bus Identifier": tl.bus_id,
            "Operator": tl.operator.upper(),
            "Direction Bounds": tl.direction,
            "Departure": _hhmm(scenario.reference_time, tl.departure_min),
            "Charging Sequence": stations_str,
            "Full Analytical Timeline": schedule_str,
            "Total Delay": f"{tl.total_wait_min} min",
            "Terminal Arrival": _hhmm(scenario.reference_time, tl.arrival_at_destination_min),
        })

    # Standard width layout to keep horizontal render trees stable
    st.dataframe(rows, hide_index=True)


def _render_station(station: str, scenario: Scenario, sched: Schedule) -> None:
    cfg = scenario.stations[station]
    order = sched.order_at(station)

    # Informational subtitle bar
    st.caption(
        f"Capacity Allocation Matrix: {cfg.chargers} Hardware Unit(s)  |  "
        f"Total Allocated Sessions: {len(order)}  |  "
        f"Aggregate Station Delay: {sum(e.wait_min for e in order)} min"
    )

    if not order:
        st.info("Station Bypassed: No allocation windows requested by current scheduling matrix.")
        return

    rows = []
    for i, e in enumerate(order, 1):
        rows.append({
            "Sequence Block": f"#{i:02d}",
            "Bus Identifier": e.bus_id,
            "Operator": e.operator.upper(),
            "Direction Bounds": e.direction,
            "Arrival Window": _hhmm(scenario.reference_time, e.arrive_min),
            "Hook Time": _hhmm(scenario.reference_time, e.start_min),
            "Disconnection Time": _hhmm(scenario.reference_time, e.end_min),
            "Queue Bottleneck": f"{e.wait_min} min" if e.wait_min > 0 else "0 min (Clear)"
        })
    # Absolute width bounds here prevent infinite measurement loop within expanders
    st.dataframe(rows, hide_index=True)


# ──────────────────────────────────────────────
# Main Application Environment
# ──────────────────────────────────────────────
def main() -> None:
    st.set_page_config(
        page_title="Intercity Fleet Charging Optimizer",
        layout="wide",
        initial_sidebar_state="expanded"
    )
    
    # Clean CSS modifications that avoid measuring conflicts with Streamlit Core
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
    
    # Institutional Header Matrix
    st.title("Intercity EV Fleet Charging Scheduler")
    st.caption("Centralized Multi-Operator Constraint Optimization Platform  |  Powered by Google OR-Tools CP-SAT")
    st.markdown("---")

    paths = list_scenarios(SCENARIOS_DIR)
    if not paths:
        st.error(f"Critical System Fault: No scenario definitions found inside directory scope: {SCENARIOS_DIR}.")
        return

    name_to_path = {p.stem: p for p in paths}
    
    # Control Panel Layout Partitioning
    with st.sidebar:
        st.markdown("### Grid Control Center")
        st.caption("Select active corridor departure manifests to run the linear resolution matrix.")
        
        pick = st.selectbox(
            "Active Operational Scenario Profile",
            list(name_to_path.keys()),
            format_func=lambda s: s.replace("_", " ").title(),
        )
        
        st.markdown("---")
        st.markdown("##### Infrastructure Route Context")
        st.caption("**Corridor Track:** Bi-directional Hub Network")
        st.caption("**Endpoints:** Bengaluru Depot ⇄ Kochi Terminal")

    # Pipeline Computation Triggers
    scenario, sched, solve_time = _solve_cached(str(name_to_path[pick]))
    
    # Layout Segregation using Structured Container blocks
    view_selector = st.radio(
        "Active System Matrix View",
        ["System Performance Analytics", "Fleet Timetable View", "Charging Node Infrastructure Queues"],
        horizontal=True
    )
    st.markdown("---")

    if view_selector == "System Performance Analytics":
        _render_summary(scenario, sched, solve_time)
        
        st.markdown("---")
        st.markdown("#### Operator Performance Matrices")
        op_rows = [
            {"Transit Operator Entity": op.upper(), "Cumulative Infrastructure Delay": f"{wait} min"}
            for op, wait in sched.total_wait_by_operator().items()
        ]
        st.dataframe(op_rows, hide_index=True)

        st.markdown("---")
        st.markdown("#### Source Scenario Parameter Profiler")
        _render_scenario_view(scenario)

    elif view_selector == "Fleet Timetable View":
        st.markdown("#### Comprehensive Vehicle Manifest & Dispatch Log")
        st.caption("Chronological tracking showing node insertion windows and continuous constraint satisfaction profiles.")
        _render_bus_timetable(scenario, sched)

    elif view_selector == "Charging Node Infrastructure Queues":
        st.markdown("#### Physical Station Queue Tracking Tables")
        st.caption("Granular tracking per node validating non-overlapping physical resource boundaries.")
        
        station_names = list(scenario.stations.keys())
        
        # Upper level spatial health summaries
        summary_cols = st.columns(len(station_names))
        for col, station in zip(summary_cols, station_names):
            with col:
                events = sched.order_at(station)
                st.metric(
                    label=f"Node {station} Load", 
                    value=f"{len(events)} Sessions", 
                    help=f"Total active connections handled at Station {station}."
                )
        
        st.markdown("---")
        
        # Clean expandable accordions for station timelines
        for station in station_names:
            with st.expander(f"⚙️ Node Station {station} Timeline Manifest", expanded=False):
                _render_station(station, scenario, sched)


if __name__ == "__main__":
    main()