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

def _metric_card(title: str, value: str) -> str:
    return f"""
    <div style="
        background:#F2A600;
        color:#000000;
        padding:16px;
        border-radius:16px;
        text-align:center;
        font-weight:600;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        min-height:90px;
        display:flex;
        flex-direction:column;
        justify-content:center;
    ">
        <div style="font-size:20px; opacity:0.85; font-weight:500;">{title}</div>
        <div style="font-size:30px; margin-top:6px;">{value}</div>
    </div>
    """


# ──────────────────────────────────────────────
# Renderers
# ──────────────────────────────────────────────
def _render_scenario_view(scenario: Scenario) -> None:
    tab_table, tab_json = st.tabs(["Readable table", "Raw JSON"])

    with tab_table:
        forward_dir = f"{scenario.route.endpoints[0]}->{scenario.route.endpoints[1]}"
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("Total buses", len(scenario.buses))
        c2.metric("B → K",
                  sum(1 for b in scenario.buses if b.direction == forward_dir))
        c3.metric("K → B",
                  sum(1 for b in scenario.buses if b.direction != forward_dir))
        c4.metric("Operators", len({b.operator for b in scenario.buses}))
        c5.metric("Stations", len(scenario.stations))
        
        st.info(
                f"""
            **Reference time:** `{scenario.reference_time}` . **Charge time:** {scenario.charge_minutes} min . **Range:** {scenario.battery_range_km} km  . **Speed:** {scenario.speed_kmph} km/hr  

            ---

            **Route segments (km):**  {", ".join(f"{s.from_node}↔{s.to_node} ({s.distance_km} km)" for s in scenario.route.segments)}

            ---

            **Stations / chargers:**  {"  ·  ".join(f"{n}: {cfg.chargers}" for n, cfg in scenario.stations.items())}

            ---

            **Weights:**  individual: `{scenario.weights.individual}` · operator: `{scenario.weights.operator}` · overall: `{scenario.weights.overall}`
            """
            )

        

        rows = [
            {
                "Bus": b.id,
                "Operator": b.operator,
                "Direction": b.direction,
                "Departure": _hhmm(scenario.reference_time, b.departure_min),
            }
            for b in scenario.buses
        ]
        st.dataframe(rows, hide_index=True, use_container_width=True)

    with tab_json:
        st.json(scenario.raw)


def _render_summary(
    scenario: Scenario,
    sched: Schedule,
    solve_time: float
) -> None:

    st.markdown("## ⚡ Optimization Results")

    if sched.status == "OPTIMAL":
        st.markdown("""
            <div style="
                background:#F2A600;
                color:#000;
                padding:14px;
                border-radius:14px;
                font-weight:800;
                text-align:center;
                margin-bottom:15px;
            ">
            ✓ Optimal Schedule Found
            </div>
        """, unsafe_allow_html=True)

    elif sched.status == "FEASIBLE":
        st.warning("Feasible schedule found")
    else:
        st.error(sched.status)

    c1, c2, c3, c4, c5 = st.columns(5)

    with c1:
        st.markdown(_metric_card("Total Wait", f"{sched.total_wait_min} min"), unsafe_allow_html=True)

    with c2:
        st.markdown(
            _metric_card(
                "Avg Wait / Bus",
                f"{sched.total_wait_min / max(len(scenario.buses),1):.1f} min"
            ),
            unsafe_allow_html=True
        )

    with c3:
        st.markdown(
            _metric_card(
                "Objective",
                f"{sched.objective:,.0f}"
            ),
            unsafe_allow_html=True
        )

    with c4:
        st.markdown(
            _metric_card(
                "Solve Time",
                f"{solve_time}s"
            ),
            unsafe_allow_html=True
        )

    with c5:
        st.markdown(
            _metric_card(
                "Charging Sessions",
                str(sched.total_charge_min // scenario.charge_minutes)
            ),
            unsafe_allow_html=True
        )

    # op_waits = sched.total_wait_by_operator()

    # if op_waits:
    #     st.markdown(
    #         "<div style='margin-top:10px; font-weight:400;'>"
    #         "Wait by operator: "
    #         + " · ".join(
    #             f"{op}: {w} min"
    #             for op, w in sorted(op_waits.items())
    #         )
    #         + "</div>",
    #         unsafe_allow_html=True
    #     )

def _render_bus_timetable(scenario: Scenario, sched: Schedule) -> None:
    """Per-bus timeline: departure → each charge (station, arrive, wait, end) → arrival."""
    rows = []
    for b in scenario.buses:
        tl = sched.by_bus.get(b.id)
        if tl is None:
            rows.append({
                "Bus": b.id,
                "Operator": b.operator,
                "Direction": b.direction,
                "Departure": _hhmm(scenario.reference_time, b.departure_min),
                "Stations": "—",
                "Schedule": "no feasible plan",
                "Total wait": "—",
                "Arrival": "—",
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
            "Bus": tl.bus_id,
            "Operator": tl.operator,
            "Direction": tl.direction,
            "Departure": _hhmm(scenario.reference_time, tl.departure_min),
            "Stations": stations_str,
            "Schedule": schedule_str,
            "Total wait": f"{tl.total_wait_min} min",
            "Arrival": _hhmm(scenario.reference_time, tl.arrival_at_destination_min),
        })

    st.dataframe(rows, hide_index=True, use_container_width=True)


def _render_station(station: str, scenario: Scenario, sched: Schedule) -> None:
    cfg = scenario.stations[station]
    order = sched.order_at(station)

    st.subheader(f"Station {station}")
    st.info(
        f"{cfg.chargers} charger  ·  {len(order)} charges  ·  "
        f"Total wait at station: {sum(e.wait_min for e in order)} min"
    )

    if not order:
        st.info("No buses charged at this station.")
        return

    rows = []
    for i, e in enumerate(order, 1):
        rows.append({
            "S.No": i,
            "Bus": e.bus_id,
            "Operator": e.operator,
            "Direction": e.direction,
            "Arrives": _hhmm(scenario.reference_time, e.arrive_min),
            "Charge start": _hhmm(scenario.reference_time, e.start_min),
            "Charge end": _hhmm(scenario.reference_time, e.end_min),
            # "wait": f"{e.wait_min} min",
            "Wait": (
                "🟢 0 min"
                if e.wait_min == 0
                else f"🔴 {e.wait_min} min"
            )
        })
    st.dataframe(rows, hide_index=True, use_container_width=True)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    st.set_page_config(page_title="Bus Charging Scheduler", layout="wide")
    
    st.markdown("""
        <style>

        /* Main App */
        .stApp {
            background-color: #F5F6F8;
            color: #000000;
        }

        /* Main content container */
        .block-container {
            max-width: 1400px;
            padding-top: 4rem;
        }

        /* Hide Streamlit branding */
        #MainMenu {visibility:hidden;}
        footer {visibility:hidden;}

        /* Hero section */
        .hero-card {
            background: #F2A600;
            color: #000000;
            border-radius: 24px;
            padding: 32px;
            margin-bottom: 24px;
            box-shadow: 0px 6px 16px rgba(0,0,0,0.15);
        }

        /* Section titles */
        .section-title {
            color: #000000;
            font-size: 28px;
            font-weight: 700;
            margin-top: 10px;
            margin-bottom: 16px;
        }

        /* Metrics */
        [data-testid="metric-container"] {
            background: #FFFFFF;
            border: 2px solid #000000;
            border-radius: 18px;
            padding: 18px;
            box-shadow: 0px 4px 10px rgba(0,0,0,0.08);
        }

        /* Metric labels */
        [data-testid="metric-container"] label {
            color: #555555;
        }

        /* Dataframes */
        [data-testid="stDataFrame"] {
            background: white;
            border-radius: 16px;
            border: 2px solid black;
            overflow: hidden;
        }

        /* Tabs */
        .stTabs [data-baseweb="tab"] {
            font-weight: 600;
            color: black;
        }

        /* Expander */
        .streamlit-expanderHeader {
            font-weight: 700;
            color: black;
        }

        /* Selectbox */
        .stSelectbox > div > div {
            border-radius: 12px;
        }

        /* Green badge replacement */
        .energy-badge {
            background: #F2A600;
            color: black;
            padding: 8px 16px;
            border-radius: 999px;
            font-weight: 400;
            display: inline-block;
        }

        /* Divider */
        hr {
            border-color: black;
        }

        </style>
        """, unsafe_allow_html=True)
    
    # st.title("Bus Charging Scheduler")
    
    st.markdown("""
        <div class="hero-card">

        <h1 style="margin-bottom:8px;color:#000000;">
        🚌🔋 Bus Charging Scheduler
        </h1>

        <p style="font-size:18px;color:#000000;">
        Optimize EV bus charging schedules using OR-Tools CP-SAT
        </p>

        <div class="energy-badge">
        Route-aware • Charger-aware • Operator-aware
        </div>

        </div>
        """, unsafe_allow_html=True)
    
    st.markdown(
        """
        <style>

        /* DataFrame header */
        div[data-testid="stDataFrame"] thead tr th {
            background-color: #F2A600 !important;
            color: #000000 !important;
            font-weight: 800 !important;
            text-align: center !important;
            border-bottom: 2px solid #000000 !important;
        }

        /* DataFrame body */
        div[data-testid="stDataFrame"] tbody tr td {
            color: #000000 !important;
        }

        /* Optional: improve spacing */
        div[data-testid="stDataFrame"] {
            border-radius: 12px;
            overflow: hidden;
        }

        </style>
        """,
        unsafe_allow_html=True
    )

    paths = list_scenarios(SCENARIOS_DIR)
    if not paths:
        st.error(f"No scenario files found in {SCENARIOS_DIR}.")
        return

    name_to_path = {p.stem: p for p in paths}
    # pick = st.selectbox(
    #     "Select scenario",
    #     list(name_to_path.keys()),
    #     format_func=lambda s: s.replace("_", " ").title(),
    # )
    
    st.markdown("### Scenario Selection")
    
    st.markdown(
    """
    <style>

    /* Selectbox container */
    div[data-baseweb="select"] > div {
        background-color: white !important;
        border-radius: 24px !important;
        # box-shadow: 0 4px 12px rgba(0,0,0,0.15) !important;
        border: 1px solid #E5E7EB !important;
        # padding: 6px 12px !important;
    }

    /* Dropdown text */
    div[data-baseweb="select"] div {
        color: black !important;
        font-weight: 400;
    }

    /* Dropdown menu */
    ul[role="listbox"] {
        background: white !important;
        border-radius: 12px !important;
        box-shadow: 0 10px 25px rgba(0,0,0,0.2) !important;
    }

    </style>
    """,
    unsafe_allow_html=True
    )

    pick = st.selectbox(
        "",
        list(name_to_path.keys()),
        format_func=lambda s: s.replace("_", " ").title(),
    )

    scenario, sched, solve_time = _solve_cached(str(name_to_path[pick]))
    
    # st.info(
    # "🛣️ Route: " +
    # " → ".join(scenario.route.nodes)
    # )
    st.markdown(
    f"""
    <div style="
        background:white;
        padding:16px;
        border-radius:24px;
        border:0.5px solid black;
        margin-bottom:20px;
        font-weight:600;
        text-align:center;
    ">
        🛣️ <b> Bi-directional Route</b><br>
        Bengaluru ↔ A ↔ B ↔ C ↔ D ↔ Kochi
    </div>
    """,
    unsafe_allow_html=True,
    )
    
    st.markdown(
    """
    <style>
    [data-testid="stDataFrame"] thead tr th {
        background-color: #F2A600 !important;
        color: #000000 !important;
        font-weight: 700 !important;
    }

    [data-testid="stDataFrame"] tbody tr td {
        color: #000000 !important;
    }
    </style>
    """,
    unsafe_allow_html=True
    )

    st.divider()
    st.markdown("### Scenario data")
    _render_scenario_view(scenario)

    st.divider()
    st.markdown("### Schedule summary")
    _render_summary(scenario, sched, solve_time)
    
    st.markdown("### Operator Performance")

    op_rows = [
        {
            "Operator": op,
            "Total Wait (min)": wait
        }
        for op, wait in sched.total_wait_by_operator().items()
    ]

    st.dataframe(
        op_rows,
        use_container_width=True,
        hide_index=True
    )

    st.divider()
    st.markdown("### Per-bus timetable")
    _render_bus_timetable(scenario, sched)
    
    st.markdown("### Charging Network")

    station_names = list(scenario.stations.keys())

    cols = st.columns(len(station_names))

    for col, station in zip(cols, station_names):
        with col:

            events = sched.order_at(station)

            st.markdown(
            _metric_card(
                station,
                f"{len(events)} sessions"
            ),
            unsafe_allow_html=True
            )

    st.divider()
    st.markdown("### Charging order by station")
    station_names = list(scenario.stations.keys())
    cols = st.columns(max(len(station_names), 1))
    # for col, station in zip(cols, station_names):
    #     with col:
    #         _render_station(station, scenario, sched)
    
    for station in station_names:
        with st.expander(
            f"⚡ Station {station}",
            expanded=False
        ):
            _render_station(
                station,
                scenario,
                sched
            )


if __name__ == "__main__":
    main()
