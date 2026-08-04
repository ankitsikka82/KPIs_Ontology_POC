"""
Capacity Tool — 6-node hub-and-spoke network (centers 1,2 -> hub 3 <-> hub 4
<- centers 5,6), strict load plan.

The agreed architecture, working end to end:
  INPUTS   network + load plan, assets, achieved rates, demand (editable below)
  ENGINE   constraint ledger; capacity = first resource to bind (theta)
  OUTPUTS  company capacity statement | over/under map | sellable headroom
           by origin | ranked levers with capacity-per-unit and per-dollar
"""

import pandas as pd
import streamlit as st

import cap6_engine as ce

st.set_page_config(page_title="Capacity Tool (6-node)", layout="wide")
st.title("Capacity Tool — 6-Node Hub & Spoke")
st.markdown(
    "Centers **1, 2** feed **hub 3**; centers **5, 6** feed **hub 4**; hubs are "
    "joined by the backbone. Strict designed routing. Every asset is one line "
    "in a constraint ledger; network capacity is where the first line binds.")


def _heat(v):
    if pd.isna(v):
        return ""
    if v >= 1.0:
        return "background-color:#f8d7da;color:#842029;font-weight:bold"
    if v >= 0.85:
        return "background-color:#fff3cd;color:#664d03"
    if v >= 0.5:
        return "background-color:#fefce8;color:#3f6212"
    return "background-color:#d1e7dd;color:#0f5132"


# ------------------------------ inputs -------------------------------------
with st.expander("Inputs — demand, assets, rates (all editable, live recompute)"):
    st.markdown("**Daily pickups by location** (OD mix = gravity split; "
                "swap in a real OD matrix in production)")
    cols = st.columns(6)
    pickups = {n: cols[i].number_input(f"Loc {n}" + (" (hub)" if n in ce.HUBS else ""),
                                       0, 2000, ce.PICKUPS[n])
               for i, n in enumerate(sorted(ce.PICKUPS))}
    st.markdown("**Assets per location**")
    resources = {}
    for n in sorted(ce.DEFAULT_RESOURCES):
        d = ce.DEFAULT_RESOURCES[n]
        c0, c1, c2, c3, c4 = st.columns([1, 1, 1, 1, 1])
        c0.markdown(f"**Loc {n}**" + ("  \n*hub*" if n in ce.HUBS else ""))
        resources[n] = dict(
            routes=c1.number_input(f"P&D routes {n}", 1, 100, d["routes"]),
            dock=c2.number_input(f"Dock workers {n}", 1, 100, d["dock"]),
            doors=c3.number_input(f"Doors {n}", 1, 100, d["doors"]),
            lh_drv=c4.number_input(f"LH drivers {n}", 1, 100, d["lh_drv"]))
    st.markdown("**Achieved rates** (validate against actuals, not nameplate)")
    r1, r2, r3 = st.columns(3)
    rates = dict(ce.DEFAULT_RATES)
    rates["shp_per_route_day"] = r1.number_input("Shipments / route-day",
                                                 5.0, 60.0,
                                                 rates["shp_per_route_day"])
    rates["shp_per_dock_hr"] = r2.number_input("Shipments / dock worker-hr",
                                               2.0, 30.0,
                                               rates["shp_per_dock_hr"])
    rates["dock_hours"] = r3.number_input("Dock processing window (hrs)",
                                          4.0, 24.0, rates["dock_hours"])

demand = ce.build_demand(pickups)
ledger, theta, summ = ce.build_ledger(demand, resources, rates)

# ------------------------------ capacity statement -------------------------
st.divider()
over = theta < 1.0
m1, m2, m3, m4 = st.columns(4)
m1.metric("End-to-end network capacity", f"{summ['sustainable']:,.0f} shp/day",
          help="Max daily shipments servable at the CURRENT freight mix — "
               "pickup through delivery, not pickup alone.")
m2.metric("Demand today", f"{summ['total_shp']:,.0f} shp/day",
          delta=f"{summ['total_shp'] - summ['sustainable']:+,.0f} vs capacity",
          delta_color="inverse")
m3.metric("Network utilization", f"{100 * summ['network_util']:.1f}%")
m4.metric("Binding constraint", summ["binder"])

if over:
    st.error(
        f"**Over capacity.** Today's {summ['total_shp']:,.0f} shipments exceed "
        f"sustainable capacity of {summ['sustainable']:,.0f}. The gap is being "
        f"absorbed by overtime, delayed freight, or service risk at: "
        f"{summ['binder']}.")
else:
    st.success(
        f"**Capacity statement:** at the current freight mix, this network can "
        f"move **{summ['sustainable']:,.0f} shipments/day** end-to-end "
        f"({100 * summ['network_util']:.0f}% utilized today, "
        f"{summ['sustainable'] - summ['total_shp']:,.0f} shp/day of headroom). "
        f"First constraint to break: **{summ['binder']}**.")

tab_map, tab_sell, tab_lever, tab_ledger, tab_method = st.tabs(
    ["Over / under map", "Sellable headroom", "Levers", "Full ledger",
     "Methodology"])

# ------------------------------ over/under map -----------------------------
with tab_map:
    st.markdown("**Facilities** — utilization by resource layer")
    tv = ledger[ledger["kind"].isin(
        ["P&D routes", "Dock labor", "Doors", "LH drivers"])]
    piv = tv.pivot_table(index="loc", columns="kind", values="util")
    piv.index = [f"Loc {i}" + (" (hub)" if i in ce.HUBS else "")
                 for i in piv.index]
    st.dataframe(piv[["P&D routes", "Dock labor", "Doors", "LH drivers"]]
                 .style.format("{:.0%}").map(_heat), width="stretch")
    st.caption("Doors and LH drivers are consumed by the trailer schedule and "
               "bind only when trailers are added.")
    st.markdown("**Lanes** — cube and weight utilization (binds on whichever "
                "hits first)")
    lc = ledger[ledger["kind"] == "Lane cube"].set_index("loc")
    lw = ledger[ledger["kind"] == "Lane weight"].set_index("loc").reindex(lc.index)
    lanes = pd.DataFrame({
        "cube util": lc["util"], "weight util": lw["util"],
        "binds on": (lw["util"] > lc["util"]).map({True: "Weight",
                                                  False: "Cube"}),
        "headroom (x)": (1 / pd.concat([lc["util"], lw["util"]], axis=1)
                         .max(axis=1)).round(2)}).sort_values("headroom (x)")
    st.dataframe(lanes.style.format({"cube util": "{:.0%}",
                                     "weight util": "{:.0%}"})
                 .map(_heat, subset=["cube util", "weight util"]),
                 width="stretch")

# ------------------------------ sellable headroom --------------------------
with tab_sell:
    st.markdown(
        "**How many more pickups can sales sell at each origin, today?** "
        "Holding every other location's freight constant, this scales one "
        "origin's mix until the first constraint on its journeys binds. This "
        "is the commercial-facing view of capacity.")
    hr = ce.pickup_headroom(demand, resources, rates)
    hr.columns = ["Origin", "Pickups today", "Sellable extra (shp/day)",
                  "First constraint hit"]
    st.dataframe(hr, hide_index=True, width="stretch")
    st.caption(
        "Note these are marginal, one-origin-at-a-time numbers — they do NOT "
        "sum. Selling headroom at one origin consumes shared hub and lane "
        "capacity that other origins were counting on.")

# ------------------------------ levers -------------------------------------
with tab_lever:
    st.markdown(
        "**What buys capacity, ranked.** Each candidate lever is applied one "
        "unit at a time and network capacity recomputed. Zero rows are as "
        "important as the top rows: investment away from the binding "
        "constraint buys nothing. *(Dollar costs are illustrative "
        "placeholders — replace with real unit economics.)*")
    lv = ce.rank_levers(demand, resources, rates)
    st.dataframe(lv, hide_index=True, width="stretch",
                 column_config={
                     "cost_usd_day": st.column_config.NumberColumn(
                         "cost $/day (placeholder)", format="$%d"),
                     "extra_shp_day": st.column_config.NumberColumn(
                         "capacity gained (shp/day)"),
                     "shp_per_$100": st.column_config.NumberColumn(
                         "shp per $100/day")})
    st.caption(
        "Lever families: Units (assets & people), Rates (productivity), Time "
        "(processing windows), Structure (load plan changes — e.g. bypass "
        "loads; requires the LP extension, shown in earlier analysis).")

# ------------------------------ full ledger --------------------------------
with tab_ledger:
    view = ledger[ledger["used"] > 0].copy()
    st.dataframe(
        view[["resource", "kind", "cap", "used", "unit", "util", "theta_r"]],
        hide_index=True, width="stretch",
        column_config={
            "util": st.column_config.ProgressColumn(
                "utilization", min_value=0.0, max_value=1.5, format="%.0f%%"),
            "cap": st.column_config.NumberColumn("capacity", format="%.0f"),
            "used": st.column_config.NumberColumn("used", format="%.0f"),
            "theta_r": st.column_config.NumberColumn("theta_r",
                                                     format="%.3f")})

# ------------------------------ methodology --------------------------------
with tab_method:
    st.markdown("""
**Capacity definition.** End-to-end capacity at the current freight mix: the
largest uniform scaling of today's OD demand servable pickup-through-delivery.
Published as shipments/day with the mandatory qualifier *"at current mix"* —
the number legitimately changes when the mix changes.

**Consumption model.** Each shipment consumes: a P&D route + dock touch at
origin; cube AND weight on every lane of its designed path; two dock touches
at every hub it transfers through (unload + reload) and no hub P&D; a dock
touch + P&D route at destination. Hubs therefore carry the whole network's
transfer freight — which is why hub dock labor binds first here.

**Strict load plan.** The tree topology makes every designed route unique, so
capacity is closed-form (no optimizer). Authorized alternates (bypass loads)
turn routing into a choice and require the multicommodity-flow LP — that is
the same engine scaled to 400 nodes, with this closed-form as its special case.

**Sellable headroom.** Marginal per-origin numbers against shared capacity;
they do not sum across origins.

**Honesty notes.** Doors/drivers are schedule-consumed (utilization shown,
bind on schedule change). Demand here is synthetic (gravity split + seeded
density variation); production swaps in the real OD matrix by day. Rates must
be achieved, not nameplate. Lever costs are placeholders.
""")
