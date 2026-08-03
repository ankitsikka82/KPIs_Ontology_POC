"""
capacity_engine.py — resource-layer capacity model for the LTL POC network.

Everything is computed on a WEEKLY basis from the same CSVs the main app uses:
  shipments.csv          -> OD demand (cube, weight, shipment count)
  planned_movements.csv  -> the load plan (strict designed legs, via SGF)
  lane_ref.csv           -> linehaul schedule (SCHED_PER_WK) per directed lane

Core idea: every resource is one linear constraint
    consumption_per_unit x flow  <=  units x efficiency
Capacity theta = max uniform scaling of current demand before the first
demand-scaled constraint binds. Doors and linehaul drivers are consumed by
the SCHEDULE (which runs regardless of fill), so they are reported as
utilization but excluded from theta until a schedule change adds trailers.

Trailer physicals match the POC data: 2,000 ft^3 cube / 20,000 lb (pups).
"""

import numpy as np
import pandas as pd

TRAILER_CUBE = 2000.0
TRAILER_WGT = 20000.0

DEFAULT_RATES = dict(
    shp_per_route_day=23.0,   # 18 stops x ~1.3 shipments/stop
    shp_per_dock_hr=10.0,
    dock_hours=8.0,
    turns_per_door_day=2.0,
    moves_per_driver_day=1.0,
    op_days_per_week=5.0,
)

DEFAULT_RESOURCES = {   # per terminal: P&D routes, dock workers, doors, LH drivers
    # doors/drivers sized to lane_ref's weekly schedule (+1 unit slack)
    "HAR": dict(routes=1, dock=1, doors=6, lh_drv=6),
    "SGF": dict(routes=2, dock=2, doors=6, lh_drv=5),   # break terminal
    "STL": dict(routes=1, dock=1, doors=5, lh_drv=6),
    "MEM": dict(routes=1, dock=1, doors=6, lh_drv=6),
    "ATL": dict(routes=1, dock=1, doors=6, lh_drv=5),
}


def demand_slice(shipments: pd.DataFrame, basis: str = "Average week"):
    """Return (shipments subset, weeks divisor) for the chosen demand basis."""
    s = shipments.copy()
    s["SHPMT_CRT_DT"] = pd.to_datetime(s["SHPMT_CRT_DT"])
    if basis == "Peak week":
        wk = s["SHPMT_CRT_DT"].dt.isocalendar()
        s["_wk"] = wk["year"].astype(str) + "-" + wk["week"].astype(str).str.zfill(2)
        peak = s.groupby("_wk")["TOT_CUBE_FT"].sum().idxmax()
        return s[s["_wk"] == peak].drop(columns="_wk"), 1.0
    span_days = (s["SHPMT_CRT_DT"].max() - s["SHPMT_CRT_DT"].min()).days
    return s, max(span_days / 7.0, 1.0)


def build_ledger(shipments, movements, lane_ref, resources=None, rates=None,
                 basis="Average week", mult=1.0):
    """Return (ledger DataFrame, theta, summary dict)."""
    res = resources or DEFAULT_RESOURCES
    r = {**DEFAULT_RATES, **(rates or {})}
    ship, weeks = demand_slice(shipments, basis)

    legs = movements.merge(
        ship[["SHPMT_NBR", "TOT_CUBE_FT", "TOT_WGT_LB"]], on="SHPMT_NBR",
        how="inner")
    lane_flow = (legs.groupby(["ORIG_TRML_CD", "DEST_TRML_CD"])
                 .agg(cube=("TOT_CUBE_FT", "sum"), wgt=("TOT_WGT_LB", "sum"),
                      shp=("SHPMT_NBR", "count")).reset_index())
    lane_flow[["cube", "wgt", "shp"]] /= weeks

    terminals = sorted(res.keys())
    pu = ship.groupby("ORIG_TRML_CD")["SHPMT_NBR"].count() / weeks
    dl = ship.groupby("DEST_TRML_CD")["SHPMT_NBR"].count() / weeks
    loads = legs.groupby("ORIG_TRML_CD")["SHPMT_NBR"].count() / weeks
    unloads = legs.groupby("DEST_TRML_CD")["SHPMT_NBR"].count() / weeks
    sched_out = lane_ref.groupby("ORIG_TRML_CD")["SCHED_PER_WK"].sum()
    sched_in = lane_ref.groupby("DEST_TRML_CD")["SCHED_PER_WK"].sum()

    rows = []
    for n in terminals:
        q = res[n]
        rows.append(dict(resource=f"{n} P&D routes", kind="P&D routes", loc=n,
                         cap=q["routes"] * r["shp_per_route_day"] * r["op_days_per_week"],
                         used=(pu.get(n, 0) + dl.get(n, 0)) * mult,
                         unit="shp/wk", scales=True))
        rows.append(dict(resource=f"{n} dock labor", kind="Dock labor", loc=n,
                         cap=q["dock"] * r["shp_per_dock_hr"] * r["dock_hours"]
                             * r["op_days_per_week"],
                         used=(loads.get(n, 0) + unloads.get(n, 0)) * mult,
                         unit="shp/wk", scales=True))
        rows.append(dict(resource=f"{n} doors", kind="Doors", loc=n,
                         cap=q["doors"] * r["turns_per_door_day"] * r["op_days_per_week"],
                         used=sched_out.get(n, 0) + sched_in.get(n, 0),
                         unit="trl spots/wk", scales=False))
        rows.append(dict(resource=f"{n} LH drivers", kind="LH drivers", loc=n,
                         cap=q["lh_drv"] * r["moves_per_driver_day"] * r["op_days_per_week"],
                         used=sched_out.get(n, 0),
                         unit="moves/wk", scales=False))

    for _, ln in lane_ref.iterrows():
        o, d, sched = ln["ORIG_TRML_CD"], ln["DEST_TRML_CD"], ln["SCHED_PER_WK"]
        fl = lane_flow[(lane_flow.ORIG_TRML_CD == o) & (lane_flow.DEST_TRML_CD == d)]
        cube = float(fl["cube"].iloc[0]) if len(fl) else 0.0
        wgt = float(fl["wgt"].iloc[0]) if len(fl) else 0.0
        rows.append(dict(resource=f"{o}->{d} cube", kind="Lane cube", loc=f"{o}->{d}",
                         cap=sched * TRAILER_CUBE, used=cube * mult,
                         unit="ft3/wk", scales=True))
        rows.append(dict(resource=f"{o}->{d} weight", kind="Lane weight", loc=f"{o}->{d}",
                         cap=sched * TRAILER_WGT, used=wgt * mult,
                         unit="lb/wk", scales=True))

    led = pd.DataFrame(rows)
    led["util"] = led["used"] / led["cap"]
    led["theta_r"] = np.where(led["scales"] & (led["used"] > 0),
                              led["cap"] / led["used"], np.inf)
    led = led.sort_values("theta_r").reset_index(drop=True)

    scaled = led[led["scales"] & (led["used"] > 0)]
    theta = float(scaled["theta_r"].min()) if len(scaled) else np.inf
    binder = scaled.iloc[0]["resource"] if len(scaled) else "none"
    wk_shp = float(ship["SHPMT_NBR"].count() / weeks)
    wk_cube = float(ship["TOT_CUBE_FT"].sum() / weeks)
    summary = dict(theta=theta, binder=binder, weeks=weeks,
                   weekly_shipments=wk_shp, weekly_cube=wk_cube,
                   sustainable_shipments=theta * wk_shp / max(mult, 1e-9) * mult,
                   network_util=1.0 / theta if theta > 0 else np.nan)
    return led, theta, summary


def growth_recipe(shipments, movements, lane_ref, resources=None, rates=None,
                  basis="Average week", steps=8):
    """Sequentially relieve the binding constraint by +1 unit; record capacity."""
    res = {k: dict(v) for k, v in (resources or DEFAULT_RESOURCES).items()}
    lref = lane_ref.copy()
    out = []
    _, th0, s0 = build_ledger(shipments, movements, lref, res, rates, basis)
    for step in range(1, steps + 1):
        led, th, s = build_ledger(shipments, movements, lref, res, rates, basis)
        b = led[led["scales"] & (led["used"] > 0)].iloc[0]
        kind, loc = b["kind"], b["loc"]
        extra = ""
        if kind == "P&D routes":
            res[loc]["routes"] += 1; action = f"add 1 P&D route at {loc}"
        elif kind == "Dock labor":
            res[loc]["dock"] += 1; action = f"add 1 dock worker at {loc}"
        elif kind in ("Lane cube", "Lane weight"):
            o, d = loc.split("->")
            m = (lref.ORIG_TRML_CD == o) & (lref.DEST_TRML_CD == d)
            lref.loc[m, "SCHED_PER_WK"] += 1
            action = f"add 1 weekly trailer on {loc}"
            # cascade: the new trailer consumes a door spot at both ends and a
            # driver move at the origin — add units only where THIS lane's
            # endpoints just went over
            led2, _, _ = build_ledger(shipments, movements, lref, res, rates, basis)
            fixed = led2[~led2["scales"] & (led2["util"] > 1.0)
                         & led2["loc"].isin([o, d])]
            for _, f_ in fixed.iterrows():
                if f_["kind"] == "Doors":
                    res[f_["loc"]]["doors"] += 1; extra += f" + 1 door at {f_['loc']}"
                elif f_["kind"] == "LH drivers":
                    res[f_["loc"]]["lh_drv"] += 1; extra += f" + 1 LH driver at {f_['loc']}"
        else:  # doors / drivers never bind theta directly
            break
        _, th_new, s_new = build_ledger(shipments, movements, lref, res, rates, basis)
        out.append(dict(step=step, action=action + extra,
                        relieved=b["resource"],
                        theta_before=round(th, 2), theta_after=round(th_new, 2),
                        sustainable_shp_wk=round(th_new * s_new["weekly_shipments"], 1)))
    return pd.DataFrame(out), th0, s0
