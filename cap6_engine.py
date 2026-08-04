"""
cap6_engine.py — capacity tool engine for the 6-node hub-and-spoke network.

Network: centers 1,2 -> hub 3 <-> hub 4 <- centers 5,6 (strict load plan on a
tree, so routing is unique and capacity is closed-form).

Implements the agreed architecture:
  INPUTS   network + load plan | assets | achieved rates | demand (OD mix,
           cube + weight)
  ENGINE   constraint ledger: every resource is one line; theta = first bind
  OUTPUTS  company capacity statement (end-to-end, at current mix)
           over/under map by facility and lane
           sellable pickup headroom by origin
           ranked levers (units / rates / time / structure) with
           capacity-per-unit and capacity-per-dollar

All dollar figures are ILLUSTRATIVE PLACEHOLDERS for ranking mechanics only.
"""

import numpy as np
import pandas as pd

CENTERS = ["1", "2", "5", "6"]
HUBS = ["3", "4"]
HOME = {"1": "3", "2": "3", "5": "4", "6": "4", "3": "3", "4": "4"}

TRAILER_CUBE = 3500.0   # usable ft3
TRAILER_WGT = 44000.0   # payload lb

DEFAULT_RATES = dict(
    shp_per_route_day=23.0,    # P&D: stops x shipments/stop
    shp_per_dock_hr=10.0,      # achieved cross-dock rate
    dock_hours=8.0,            # processing window (the TIME lever)
    turns_per_door_day=2.0,
    moves_per_driver_day=1.0,
)

DEFAULT_RESOURCES = {
    "1": dict(routes=8,  dock=3,  doors=4,  lh_drv=3),
    "2": dict(routes=11, dock=4,  doors=4,  lh_drv=3),
    "3": dict(routes=14, dock=11, doors=12, lh_drv=9),
    "4": dict(routes=16, dock=12, doors=12, lh_drv=10),
    "5": dict(routes=10, dock=3,  doors=4,  lh_drv=3),
    "6": dict(routes=9,  dock=3,  doors=4,  lh_drv=3),
}

DEFAULT_SCHED = {  # trailers/day per designed lane
    ("1", "3"): 2, ("3", "1"): 2, ("2", "3"): 2, ("3", "2"): 2,
    ("3", "4"): 4, ("4", "3"): 4,
    ("4", "5"): 2, ("5", "4"): 2, ("4", "6"): 2, ("6", "4"): 2,
}

PICKUPS = {"1": 80, "2": 120, "3": 150, "4": 180, "5": 100, "6": 90}

# illustrative daily costs per +1 unit of each lever (PLACEHOLDERS)
LEVER_COST = dict(route=600, dock=250, door=150, lh_drv=700, trailer=550,
                  dock_hour=300, dock_rate=400)


def build_demand(pickups=None, seed=7):
    """OD demand: gravity split of pickups, with per-OD density variation so
    some lanes cube out and some weigh out."""
    pk = pickups or PICKUPS
    rng = np.random.default_rng(seed)
    S = sum(pk.values())
    rows = []
    for o in pk:
        for d in pk:
            if o == d:
                continue
            shp = pk[o] * pk[d] / (S - pk[o])
            avg_cube = rng.uniform(45, 55)          # ft3/shipment
            avg_wgt = avg_cube * rng.uniform(9.5, 14.0)   # lb (density)
            rows.append(dict(o=o, d=d, shp=shp, cube=shp * avg_cube,
                             wgt=shp * avg_wgt))
    return pd.DataFrame(rows)


def strict_path(o, d):
    seq = [o]
    if HOME[o] != o:
        seq.append(HOME[o])
    if HOME[d] != seq[-1]:
        seq.append(HOME[d])
    if d != seq[-1]:
        seq.append(d)
    return list(zip(seq[:-1], seq[1:]))


def build_ledger(demand, resources=None, rates=None, sched=None, mult=1.0):
    res = resources or DEFAULT_RESOURCES
    r = {**DEFAULT_RATES, **(rates or {})}
    lanes = dict(sched or DEFAULT_SCHED)

    lane_shp = {a: 0.0 for a in lanes}
    lane_cube = {a: 0.0 for a in lanes}
    lane_wgt = {a: 0.0 for a in lanes}
    for _, row in demand.iterrows():
        for a in strict_path(row.o, row.d):
            lane_shp[a] += row.shp
            lane_cube[a] += row.cube
            lane_wgt[a] += row.wgt

    loads = {n: 0.0 for n in res}
    unloads = {n: 0.0 for n in res}
    for a, q in lane_shp.items():
        loads[a[0]] += q
        unloads[a[1]] += q
    pu = demand.groupby("o")["shp"].sum().to_dict()
    dl = demand.groupby("d")["shp"].sum().to_dict()
    t_out = {n: sum(t for a, t in lanes.items() if a[0] == n) for n in res}
    t_in = {n: sum(t for a, t in lanes.items() if a[1] == n) for n in res}

    rows = []
    for n, q in res.items():
        kindtag = "hub" if n in HUBS else "ctr"
        rows.append(dict(resource=f"{kindtag} {n} P&D routes", kind="P&D routes",
                         loc=n, cap=q["routes"] * r["shp_per_route_day"],
                         used=(pu.get(n, 0) + dl.get(n, 0)) * mult,
                         unit="shp/day", scales=True))
        rows.append(dict(resource=f"{kindtag} {n} dock labor", kind="Dock labor",
                         loc=n,
                         cap=q["dock"] * r["shp_per_dock_hr"] * r["dock_hours"],
                         used=(loads[n] + unloads[n]) * mult,
                         unit="shp/day", scales=True))
        rows.append(dict(resource=f"{kindtag} {n} doors", kind="Doors", loc=n,
                         cap=q["doors"] * r["turns_per_door_day"],
                         used=t_out[n] + t_in[n], unit="trl/day", scales=False))
        rows.append(dict(resource=f"{kindtag} {n} LH drivers", kind="LH drivers",
                         loc=n, cap=q["lh_drv"] * r["moves_per_driver_day"],
                         used=t_out[n], unit="moves/day", scales=False))
    for a, t in lanes.items():
        loc = f"{a[0]}->{a[1]}"
        rows.append(dict(resource=f"lane {loc} cube", kind="Lane cube", loc=loc,
                         cap=t * TRAILER_CUBE, used=lane_cube[a] * mult,
                         unit="ft3/day", scales=True))
        rows.append(dict(resource=f"lane {loc} weight", kind="Lane weight",
                         loc=loc, cap=t * TRAILER_WGT, used=lane_wgt[a] * mult,
                         unit="lb/day", scales=True))

    led = pd.DataFrame(rows)
    led["util"] = led["used"] / led["cap"]
    led["theta_r"] = np.where(led["scales"] & (led["used"] > 0),
                              led["cap"] / led["used"], np.inf)
    led = led.sort_values("theta_r").reset_index(drop=True)
    sc = led[led["scales"] & (led["used"] > 0)]
    theta = float(sc["theta_r"].min())
    total = float(demand["shp"].sum())
    summary = dict(theta=theta, binder=sc.iloc[0]["resource"],
                   total_shp=total, sustainable=theta * total,
                   network_util=1.0 / theta)
    return led, theta, summary


def pickup_headroom(demand, resources=None, rates=None, sched=None):
    """Sellable headroom by ORIGIN: max extra daily pickups at each location,
    holding all other locations' freight constant. Closed-form: for each
    constraint, headroom = slack / marginal consumption of that origin's mix."""
    led, _, _ = build_ledger(demand, resources, rates, sched)
    out = []
    for n in sorted(PICKUPS):
        sub = demand[demand.o == n]
        led_o, _, _ = build_ledger(sub, resources, rates, sched)
        contrib = led_o.set_index("resource")["used"]
        base = led.set_index("resource")
        fac, binder = np.inf, "none"
        for rname, browz in base[base["scales"]].iterrows():
            c = contrib.get(rname, 0.0)
            if c > 1e-9:
                f = (browz["cap"] - browz["used"]) / c
                if f < fac:
                    fac, binder = f, rname
        extra = fac * sub["shp"].sum()
        out.append(dict(origin=n, pickups_today=round(sub["shp"].sum(), 1),
                        sellable_extra_shp=round(max(extra, 0.0), 1),
                        first_constraint=binder))
    return pd.DataFrame(out)


def rank_levers(demand, resources=None, rates=None, sched=None):
    """Finite-difference lever ranking: +1 unit of each lever, recompute
    capacity, report delta shipments/day and (placeholder) cost efficiency."""
    res0 = {k: dict(v) for k, v in (resources or DEFAULT_RESOURCES).items()}
    r0 = {**DEFAULT_RATES, **(rates or {})}
    sched0 = dict(sched or DEFAULT_SCHED)
    _, th0, s0 = build_ledger(demand, res0, r0, sched0)
    base_cap = th0 * s0["total_shp"]
    cands = []
    for n in res0:                                   # UNITS
        for fld, nm, cost in [("routes", "P&D route", LEVER_COST["route"]),
                              ("dock", "dock worker", LEVER_COST["dock"])]:
            rr = {k: dict(v) for k, v in res0.items()}
            rr[n][fld] += 1
            _, th, s = build_ledger(demand, rr, r0, sched0)
            cands.append((f"+1 {nm} @ {n}", "Units", cost,
                          th * s["total_shp"] - base_cap))
    for a in sched0:                                 # UNITS (linehaul)
        ss = dict(sched0)
        ss[a] += 1
        _, th, s = build_ledger(demand, res0, r0, ss)
        cands.append((f"+1 trailer/day {a[0]}->{a[1]}", "Units",
                      LEVER_COST["trailer"], th * s["total_shp"] - base_cap))
    for n in HUBS:                                   # TIME: +1 hr hub window
        # window applies per-facility; approximate by scaling that hub's dock cap
        rr = {k: dict(v) for k, v in res0.items()}
        rr[n]["dock"] = rr[n]["dock"] * (r0["dock_hours"] + 1) / r0["dock_hours"]
        _, th, s = build_ledger(demand, rr, r0, sched0)
        cands.append((f"+1 hr dock window @ hub {n}", "Time",
                      LEVER_COST["dock_hour"], th * s["total_shp"] - base_cap))
    for n in HUBS:                                   # RATES: +1 shp/hr at hub
        rr = {k: dict(v) for k, v in res0.items()}
        rr[n]["dock"] = rr[n]["dock"] * (r0["shp_per_dock_hr"] + 1) / r0["shp_per_dock_hr"]
        _, th, s = build_ledger(demand, rr, r0, sched0)
        cands.append((f"+1 shp/hr dock productivity @ hub {n}", "Rates",
                      LEVER_COST["dock_rate"], th * s["total_shp"] - base_cap))
    df = pd.DataFrame(cands, columns=["lever", "family", "cost_usd_day",
                                      "extra_shp_day"])
    df["extra_shp_day"] = df["extra_shp_day"].round(1)
    df["shp_per_$100"] = (df["extra_shp_day"] / df["cost_usd_day"] * 100).round(2)
    return df.sort_values(["extra_shp_day", "shp_per_$100"],
                          ascending=False).reset_index(drop=True)
