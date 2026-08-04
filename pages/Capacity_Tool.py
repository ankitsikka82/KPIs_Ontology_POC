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


NODE_XY = {"1": (95, 85), "2": (95, 340), "3": (285, 212), "4": (465, 212),
           "5": (655, 85), "6": (655, 340)}
EDGE_PAIRS = [("1", "3"), ("2", "3"), ("3", "4"), ("4", "5"), ("4", "6")]


def _status(u):
    if u >= 1.0:
        return "#b91c1c", "over"
    if u >= 0.85:
        return "#b45309", "hot"
    return "#15803d", "ok"


def network_svg(ledger):
    """Animated network map: node color = worst facility resource, lane color
    = worst of cube/weight, dash flow speed rises with utilization, over-
    capacity elements pulse. Pure SVG+CSS — no extra dependencies."""
    node_u = {}
    for n in NODE_XY:
        rows = ledger[(ledger["loc"] == n) & ledger["scales"]]
        node_u[n] = float(rows["util"].max()) if len(rows) else 0.0
    lane_u = {}
    for _, row in ledger[ledger["kind"].isin(["Lane cube", "Lane weight"])].iterrows():
        lane_u[row["loc"]] = max(lane_u.get(row["loc"], 0.0), float(row["util"]))

    style = """<style>
      .flow { stroke-dasharray: 9 7; animation: dash linear infinite; fill: none; }
      @keyframes dash { to { stroke-dashoffset: -64; } }
      .pulse { animation: pulse 1.1s ease-in-out infinite; }
      @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
      text { font-family: -apple-system, Segoe UI, sans-serif; }
    </style>"""

    parts = [style, '<svg viewBox="0 0 750 445" width="100%" '
             'xmlns="http://www.w3.org/2000/svg">',
             '<defs><marker id="ah" viewBox="0 0 10 10" refX="8" refY="5" '
             'markerWidth="5.5" markerHeight="5.5" orient="auto">'
             '<path d="M1 1L9 5L1 9" fill="none" stroke="context-stroke" '
             'stroke-width="1.6" stroke-linecap="round"/></marker></defs>']

    for a, b in EDGE_PAIRS:
        (x1, y1), (x2, y2) = NODE_XY[a], NODE_XY[b]
        dx, dy = x2 - x1, y2 - y1
        L = (dx * dx + dy * dy) ** 0.5
        ux, uy = dx / L, dy / L
        px, py = -uy, ux                     # perpendicular for lane offset
        r1, r2 = 34, 34
        for (o, d, sgn) in [(a, b, 1), (b, a, -1)]:
            u = lane_u.get(f"{o}->{d}", 0.0)
            col, stat = _status(u)
            ox, oy = px * 6 * sgn, py * 6 * sgn
            if o == a:
                sx, sy = x1 + ux * r1 + ox, y1 + uy * r1 + oy
                ex, ey = x2 - ux * r2 + ox, y2 - uy * r2 + oy
            else:
                sx, sy = x2 - ux * r2 + ox, y2 - uy * r2 + oy
                ex, ey = x1 + ux * r1 + ox, y1 + uy * r1 + oy
            speed = max(0.7, 3.2 - 2.2 * min(u, 1.3))
            width = 2 + 3 * min(u, 1.2)
            pulse = " pulse" if stat == "over" else ""
            parts.append(
                f'<line class="flow{pulse}" x1="{sx:.0f}" y1="{sy:.0f}" '
                f'x2="{ex:.0f}" y2="{ey:.0f}" stroke="{col}" '
                f'stroke-width="{width:.1f}" marker-end="url(#ah)" '
                f'style="animation-duration:{speed:.2f}s">'
                f'<title>{o}→{d}: {u:.0%} (worst of cube/weight)</title></line>')
            mx, my = (sx + ex) / 2 + px * 13 * sgn, (sy + ey) / 2 + py * 13 * sgn
            parts.append(f'<text x="{mx:.0f}" y="{my:.0f}" font-size="10.5" '
                         f'fill="{col}" text-anchor="middle">{u:.0%}</text>')

    for n, (x, y) in NODE_XY.items():
        u = node_u[n]
        col, stat = _status(u)
        hub = n in ce.HUBS
        r = 32 if hub else 26
        pulse = ' class="pulse"' if stat == "over" else ""
        parts.append(f'<circle{pulse} cx="{x}" cy="{y}" r="{r}" fill="{col}" '
                     f'fill-opacity="0.15" stroke="{col}" stroke-width="2.5">'
                     f'<title>Loc {n}: worst facility resource {u:.0%}</title></circle>')
        parts.append(f'<text x="{x}" y="{y - 4}" font-size="14" font-weight="700" '
                     f'fill="{col}" text-anchor="middle">{n}</text>')
        parts.append(f'<text x="{x}" y="{y + 12}" font-size="10.5" fill="{col}" '
                     f'text-anchor="middle">{u:.0%}</text>')
        tag = "HUB" if hub else "center"
        parts.append(f'<text x="{x}" y="{y + r + 15}" font-size="10" '
                     f'fill="#6b7280" text-anchor="middle">{tag}</text>')

    parts.append(
        '<g font-size="11" fill="#374151">'
        '<circle cx="30" cy="425" r="6" fill="#15803d" fill-opacity="0.25" '
        'stroke="#15803d"/><text x="42" y="429">under 85%</text>'
        '<circle cx="130" cy="425" r="6" fill="#b45309" fill-opacity="0.25" '
        'stroke="#b45309"/><text x="142" y="429">85–100% (hot)</text>'
        '<circle cx="260" cy="425" r="6" fill="#b91c1c" fill-opacity="0.25" '
        'stroke="#b91c1c"/><text x="272" y="429">over capacity (pulsing)</text>'
        '<text x="470" y="429">flow speed & thickness rise with utilization</text>'
        "</g></svg>")
    return "".join(parts)


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

tab_net, tab_map, tab_sell, tab_lever, tab_ledger, tab_method = st.tabs(
    ["Network view", "Over / under map", "Sellable headroom", "Levers",
     "Full ledger", "How it works"])

# ------------------------------ network view -------------------------------
with tab_net:
    st.markdown(
        "Live map of the network. **Node color** = its worst facility resource "
        "(P&D or dock); **lane color** = worst of cube/weight; freight flow "
        "animates faster and thicker as utilization rises; anything over "
        "capacity pulses red. Hover any element for detail. Change inputs "
        "above and watch the map re-color.")
    if hasattr(st, "iframe"):                       # Streamlit >= mid-2026
        st.iframe(network_svg(ledger), height=470)
    else:                                            # older runtimes
        import streamlit.components.v1 as components
        components.html(network_svg(ledger), height=470)

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

# ------------------------------ how it works (learning) --------------------
with tab_method:
    st.markdown(
        "This tab is the **replication recipe**: the exact algorithm, worked "
        "with the live numbers on screen right now, then the scale-up path "
        "to 400+ centers. Nothing here is specific to 6 nodes — only the "
        "input tables grow.")

    st.subheader("The algorithm (5 steps)")
    st.markdown("""
1. **Demand** — build today's OD matrix: shipments, cube, and weight per
   origin-destination pair.
2. **Expand along the load plan** — walk each OD down its designed route.
   Origin gets a P&D touch + a dock load; every hub crossed gets an unload
   **and** a reload (2 dock touches, no P&D); every lane traversed accumulates
   the shipment's cube and weight; destination gets a dock unload + P&D touch.
3. **Write the constraint ledger** — one row per resource:
   `capacity = units x achieved rate x processing window`, consumption from
   step 2. That single linear form covers routes, dock labor, doors, drivers,
   and both trailer dimensions.
4. **Solve** — per row, `theta_r = capacity / consumption`. Network capacity
   `theta = min(theta_r)` over demand-scaled rows; sustainable volume =
   `theta x today's shipments`. Under strict unique routing this is
   closed-form — no optimizer.
5. **Outputs** — utilization = the over/under map; the sorted theta_r column
   = the constraint ranking; +1-unit recomputes = the lever ranking;
   per-origin scaling = sellable headroom.
""")

    st.subheader("Worked example — live, with your current inputs")
    b = ledger[ledger["scales"] & (ledger["used"] > 0)].iloc[0]
    nxt = ledger[ledger["scales"] & (ledger["used"] > 0)].iloc[1]
    st.markdown(f"""
The binding constraint right now is **{b['resource']}**:

- capacity = **{b['cap']:,.0f} {b['unit']}** (units x achieved rate x window
  from the Inputs panel)
- consumption at today's demand = **{b['used']:,.0f} {b['unit']}**
- `theta_r = {b['cap']:,.0f} / {b['used']:,.0f} = {b['theta_r']:.3f}`

No other row has a smaller theta_r, so network capacity =
`{b['theta_r']:.3f} x {summ['total_shp']:,.0f} = {summ['sustainable']:,.0f}
shipments/day` — the headline above. Relieve this constraint and capacity
rises only until the next row binds (**{nxt['resource']}**,
theta_r = {nxt['theta_r']:.3f}), which is why single investments
under-deliver and the lever tab recomputes rather than extrapolates.
""")

    st.subheader("Replicating at 400+ centers")
    st.markdown("""
**Input tables (this is the whole data contract):**

| table | grain | key columns |
|---|---|---|
| `facility_master` | facility | id, type (hub/EOL), doors, dock FTEs, P&D routes, LH drivers, shift windows |
| `achieved_rates` | facility | shp/dock-hr, shp/route-day, door turns — from actuals, not nameplate |
| `lane_schedule` | directed lane | trailers/day, equipment type (cube & weight caps) |
| `load_plan` | OD pair | ordered designed route + authorized alternates |
| `od_demand` | OD x day | shipments, cube, weight |

**Scaling facts:** the ledger for 400 facilities and ~4,000 lanes is roughly
20,000 rows — step 2 is a join-and-groupby, step 4 is a column divide and a
min. Closed-form runs in seconds on a laptop; it is a nightly-batch problem,
not a compute problem. The only structural change at scale: where the load
plan authorizes **alternate routes**, capacity becomes a routing choice and
the min-ratio is replaced by a small multicommodity-flow LP (maximize theta
subject to the same ledger rows as constraints) — the closed-form is exactly
that LP's special case when every OD has one path.

**Pseudocode (production shape):**
```python
demand   = od_demand[day]                       # shp, cube, wgt per OD
flows    = expand(demand, load_plan)            # lane + facility consumption
ledger   = capacity(facility_master, achieved_rates,
                    lane_schedule) .join(flows)
theta    = (ledger.cap / ledger.used)[ledger.scales].min()
publish(theta * demand.shp.sum(),               # capacity statement
        ledger.util,                            # over/under map
        rank_levers(ledger),                    # +1-unit recomputes
        pickup_headroom(demand, ledger))        # sellable by origin
```

**Publication discipline:** run per operating day, publish against the P85
demand day, always attach *"at current freight mix"*, and validate rates
against achieved throughput before the first executive readout.
""")
