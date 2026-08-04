"""
Network Capacity — separate page for the Cube Utilization POC.

Reads the SAME CSVs as the main app and answers, for the POC network:
  - What is total network capacity (as a multiple of current demand)?
  - What % of it is used today?
  - How much more freight can we take, and which resource breaks first?
  - What is the cheapest sequence of actions to grow capacity?

Method: resource-layer model. Every resource (P&D routes, dock labor, doors,
LH drivers, lane cube, lane weight) is one linear constraint. With the strict
load plan (all inter-terminal freight via SGF, from planned_movements), routing
is unique, so capacity is closed-form: theta = min(capacity/consumption) over
demand-scaled resources. Doors and drivers are consumed by the fixed schedule,
so they show utilization but bind only when a schedule change adds trailers.
"""

import pandas as pd
import streamlit as st

import capacity_engine as ce

st.set_page_config(page_title="Network Capacity", layout="wide")

st.title("Network Capacity")
st.markdown(
    "**How much freight can this network take?** Capacity here is not a single "
    "tonnage number — it is the maximum uniform scaling of *today's demand mix* "
    "before the first resource binds. Every lane and terminal resource is one "
    "constraint; the tightest one defines network capacity and is the first "
    "place to invest.")

TERMINAL_NAMES = {"HAR": "Harrison", "SGF": "Springfield", "STL": "Saint Louis",
                  "MEM": "Memphis", "ATL": "Atlanta"}


def _heat(v):
    """Band-based cell shading (no matplotlib dependency)."""
    if pd.isna(v):
        return ""
    if v >= 1.0:
        return "background-color:#f8d7da;color:#842029;font-weight:bold"
    if v >= 0.85:
        return "background-color:#fff3cd;color:#664d03"
    if v >= 0.5:
        return "background-color:#fefce8;color:#3f6212"
    return "background-color:#d1e7dd;color:#0f5132"



@st.cache_data
def load_data():
    return (pd.read_csv("shipments.csv"), pd.read_csv("planned_movements.csv"),
            pd.read_csv("lane_ref.csv"))


shipments, movements, lane_ref = load_data()

# ----------------------------- assumptions ---------------------------------
with st.expander("Resource assumptions (editable — the model recomputes live)"):
    st.caption(
        "Defaults are calibrated so the current linehaul schedule fits doors and "
        "drivers with one unit of slack. Efficiency rates are the levers ops "
        "would challenge first — that is by design: every number here is an "
        "explicit, tunable assumption, not a hidden constant.")
    rc1, rc2, rc3 = st.columns(3)
    rates = dict(ce.DEFAULT_RATES)
    rates["shp_per_route_day"] = rc1.number_input(
        "Shipments per P&D route-day", 5.0, 60.0, rates["shp_per_route_day"])
    rates["shp_per_dock_hr"] = rc2.number_input(
        "Shipments per dock worker-hour", 2.0, 30.0, rates["shp_per_dock_hr"])
    rates["op_days_per_week"] = rc3.number_input(
        "Operating days per week", 1.0, 7.0, rates["op_days_per_week"])

    resources = {}
    for t in sorted(ce.DEFAULT_RESOURCES):
        d = ce.DEFAULT_RESOURCES[t]
        c0, c1, c2, c3, c4 = st.columns([1.2, 1, 1, 1, 1])
        c0.markdown(f"**{TERMINAL_NAMES.get(t, t)} ({t})**"
                    + ("  \n*break terminal*" if t == "SGF" else ""))
        resources[t] = dict(
            routes=c1.number_input(f"P&D routes {t}", 1, 50, d["routes"]),
            dock=c2.number_input(f"Dock workers {t}", 1, 50, d["dock"]),
            doors=c3.number_input(f"Doors {t}", 1, 50, d["doors"]),
            lh_drv=c4.number_input(f"LH drivers {t}", 1, 50, d["lh_drv"]))

basis = st.radio("Demand basis", ["Average week", "Peak week"], horizontal=True,
                 help="Capacity should be judged against the demand you must "
                      "actually serve. Peak week is the honest planning basis; "
                      "average week is the optimistic one.")

ledger, theta, summ = ce.build_ledger(shipments, movements, lane_ref,
                                      resources, rates, basis)

# ----------------------------- headline metrics ----------------------------
m1, m2, m3, m4 = st.columns(4)
m1.metric("Effective network capacity", f"{theta:.1f}x current demand",
          help="Max uniform scaling of the current OD mix before the first "
               "demand-scaled resource binds.")
m2.metric("Network utilization", f"{100 * summ['network_util']:.0f}%",
          help="1/theta: how much of sustainable capacity today's demand uses.")
m3.metric("Sustainable volume",
          f"{theta * summ['weekly_shipments']:.0f} shp/wk",
          delta=f"{(theta - 1) * summ['weekly_shipments']:+.0f} vs today's "
                f"{summ['weekly_shipments']:.0f}")
m4.metric("Binding constraint", summ["binder"],
          help="The first resource to break as demand grows. Investment "
               "anywhere else buys zero network capacity.")

if basis == "Peak week":
    st.caption("Peak-week basis: demand from the busiest ISO week in the data.")

tab_stress, tab_term, tab_lane, tab_recipe, tab_method = st.tabs(
    ["Stress test", "Terminals", "Lanes", "Growth recipe", "Methodology"])

# ----------------------------- stress test ---------------------------------
with tab_stress:
    st.markdown(
        "Drag demand up and watch which resources go hot, in what order. "
        "The model caps out exactly at the effective-capacity multiple above.")
    mult = st.slider("Demand multiplier", 1.0, max(2.0, float(round(theta * 1.5, 1))),
                     1.0, 0.1)
    led_m, _, _ = ce.build_ledger(shipments, movements, lane_ref,
                                  resources, rates, basis, mult=mult)
    view = led_m[led_m["used"] > 0].copy()
    view["status"] = view.apply(
        lambda r: ("(schedule)" if not r["scales"] else
                   "OVER" if r["util"] > 1.0 else
                   "HOT" if r["util"] >= 0.85 else "ok"), axis=1)
    over = view[(view["scales"]) & (view["util"] > 1.0)]
    if len(over):
        st.error(f"At {mult:.1f}x demand, {len(over)} resource(s) are over "
                 f"capacity — first: {over.iloc[0]['resource']}. This demand "
                 "level is infeasible without the growth recipe.")
    st.dataframe(
        view[["resource", "kind", "cap", "used", "unit", "util", "status"]],
        hide_index=True, width="stretch",
        column_config={
            "util": st.column_config.ProgressColumn(
                "utilization", min_value=0.0, max_value=1.5, format="%.0f%%"),
            "cap": st.column_config.NumberColumn("capacity", format="%.0f"),
            "used": st.column_config.NumberColumn("used", format="%.0f")})

# ----------------------------- terminals -----------------------------------
with tab_term:
    st.markdown(
        "Terminal resources at current demand. P&D routes and dock labor scale "
        "with freight; doors and LH drivers are consumed by the *schedule* and "
        "change only when trailers are added or cut.")
    tv = ledger[ledger["kind"].isin(
        ["P&D routes", "Dock labor", "Doors", "LH drivers"])].copy()
    piv = tv.pivot_table(index="loc", columns="kind", values="util")
    piv.index = [f"{TERMINAL_NAMES.get(i, i)} ({i})" for i in piv.index]
    st.dataframe(
        piv[["P&D routes", "Dock labor", "Doors", "LH drivers"]].style.format(
            "{:.0%}").map(_heat),
        width="stretch")
    st.caption(
        "The break terminal (SGF) carries transfer freight belonging to the "
        "whole network — in real LTL networks the hub dock is usually the "
        "binding resource. In this synthetic data, linehaul weight binds first.")

# ----------------------------- lanes ---------------------------------------
with tab_lane:
    st.markdown(
        "Each lane has TWO capacities — cube and weight — and freight hits "
        "whichever comes first (the same cube-out/weigh-out logic as "
        "`CNSTRNT_CD` in the utilization fact). The binding dimension per lane:")
    lc = ledger[ledger["kind"] == "Lane cube"].set_index("loc")
    lw = (ledger[ledger["kind"] == "Lane weight"].set_index("loc")
      .reindex(lc.index))  # align to cube rows (ledger is theta-sorted)
    lanes = pd.DataFrame({
        "cube util": lc["util"], "weight util": lw["util"],
        "binds on": (lw["util"] > lc["util"]).map({True: "Weight", False: "Cube"}),
        "weekly trailers": (lc["cap"] / ce.TRAILER_CUBE).astype(int),
        "headroom (x)": (1 / pd.concat([lc["util"], lw["util"]], axis=1)
                         .max(axis=1)).round(1)})
    lanes = lanes[lc["used"] > 0].sort_values("headroom (x)")
    st.dataframe(lanes.style.format({"cube util": "{:.0%}", "weight util": "{:.0%}"})
                 .map(_heat, subset=["cube util", "weight util"]),
                 width="stretch")
    st.caption(
        "Lanes with zero planned flow (scheduled but empty under the strict "
        "load plan) are hidden — they are frequency-rationalization candidates, "
        "covered on the main page.")

# ----------------------------- growth recipe -------------------------------
with tab_recipe:
    st.markdown(
        "The capital-allocation answer: relieve the binding constraint one unit "
        "at a time and recompute. Each action buys capacity **only up to the "
        "next constraint** — this is why single-resource investments "
        "underdeliver. When adding a trailer overruns doors or drivers at the "
        "lane's endpoints, the recipe adds those units too (the cascade).")
    steps = st.slider("Recipe steps", 3, 15, 8)
    recipe, _, _ = ce.growth_recipe(shipments, movements, lane_ref,
                                    resources, rates, basis, steps=steps)
    if len(recipe):
        st.dataframe(recipe, hide_index=True, width="stretch",
                     column_config={
                         "sustainable_shp_wk": st.column_config.NumberColumn(
                             "sustainable shp/wk", format="%.0f")})
        gain = recipe.iloc[-1]["theta_after"] - recipe.iloc[0]["theta_before"]
        st.success(f"{len(recipe)} actions grow capacity from "
                   f"{recipe.iloc[0]['theta_before']:.1f}x to "
                   f"{recipe.iloc[-1]['theta_after']:.1f}x current demand "
                   f"(+{gain:.1f}x).")

# ----------------------------- methodology ---------------------------------
with tab_method:
    st.markdown("""
**Definition.** Effective capacity is the largest uniform multiple *theta* of the
current OD demand the network can serve. Utilization = 1/theta. This is the only
capacity definition that is honest about demand mix: a network with room for 30%
more Midwest freight may have 0% room for Southeast freight.

**Why closed-form works here.** The load plan is strict (every inter-terminal
shipment routes via SGF, per `planned_movements`), so each OD has a unique path
and theta = min(capacity/consumption) over all resources — no optimizer needed.
The moment alternate/bypass routing is authorized, the same resource table
becomes the constraint set of a multicommodity-flow LP that fills direct
trailers first and overflows via the break terminal. That LP is the scaling
path to a 400-node network; this page is its closed-form special case.

**Resource layers.** Per terminal: P&D routes (pickup + delivery shipments),
dock labor (shipments loaded + unloaded, so transfer freight counts twice
across its journey), doors and LH drivers (consumed by the trailer schedule).
Per lane: cube AND weight — a trailer is full at whichever limit hits first,
identical to the `CNSTRNT_CD` logic in `trlr_util_fct`.

**Stated simplifications.** (1) Doors/drivers are utilization-only until the
schedule changes; the growth recipe models that cascade explicitly. (2) Demand
is a weekly average or peak week of sparse synthetic data — with real volumes,
run this per operating day and publish capacity at the P85 demand day.
(3) Efficiency rates are editable assumptions and must be validated against
achieved (not nameplate) throughput before any real decision.
""")
