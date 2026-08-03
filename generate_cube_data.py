import pandas as pd
import numpy as np
from datetime import datetime, timedelta

np.random.seed(42)

terminals = ['HAR', 'SGF', 'STL', 'MEM', 'ATL']  # legacy terminal codes

num_shipments = 80
shipments = pd.DataFrame({
    'SHPMT_NBR': [f'SHP_{i:05d}' for i in range(1, num_shipments + 1)],
    'CUST_CD': np.random.choice([f'CUST_{i:03d}' for i in range(1, 21)], num_shipments),
    'ORIG_TRML_CD': np.random.choice(terminals, num_shipments),
    'DEST_TRML_CD': '',  # filled below, guaranteed != origin
    'SVC_TYP_CD': np.random.choice(['Economy', 'Standard', 'Priority'], num_shipments),
    'TOT_WGT_LB': np.random.randint(500, 10000, num_shipments),
    'TOT_CUBE_FT': np.random.randint(50, 800, num_shipments),
    'REV_AMT': np.random.randint(500, 5000, num_shipments),
    'SHPMT_CRT_DT': [(datetime.now() - timedelta(days=int(np.random.randint(0, 56)))).date() for _ in range(num_shipments)]
})

# Guarantee destination != origin (no self-lanes)
shipments['DEST_TRML_CD'] = [
    np.random.choice([t for t in terminals if t != o])
    for o in shipments['ORIG_TRML_CD']
]

planned_movements = []
movement_id = 1
for _, shipment in shipments.iterrows():
    origin = shipment['ORIG_TRML_CD']
    destination = shipment['DEST_TRML_CD']
    service = shipment['SVC_TYP_CD']

    if origin == destination:
        continue

    legs = [origin]
    if origin != 'SGF' and destination != 'SGF':
        legs.append('SGF')
    legs.append(destination)

    for i in range(len(legs) - 1):
        planned_movements.append({
            'MVMT_NBR': f'MOV_{movement_id:05d}',
            'SHPMT_NBR': shipment['SHPMT_NBR'],
            'LEG_SEQ_NBR': i + 1,
            'ORIG_TRML_CD': legs[i],
            'DEST_TRML_CD': legs[i + 1],
            'SVC_TYP_CD': service
        })
        movement_id += 1

planned_movements_df = pd.DataFrame(planned_movements)

num_dispatches = 30
dispatches = []
dispatch_id = 1
trailer_capacity_cube = 2000
max_weight_lbs = 20000

# Guarantee data coverage for "last week" questions: the last COMPLETE week
# (Monday through Sunday before the current week) always gets >= 5 dispatches.
today = datetime.now().date()
this_monday = today - timedelta(days=today.weekday())
last_week_monday = this_monday - timedelta(days=7)

assigned_shipments = set()
for i in range(num_dispatches):
    origin = np.random.choice(terminals)
    destination = np.random.choice([t for t in terminals if t != origin])

    eligible_shipments = planned_movements_df[
        (planned_movements_df['ORIG_TRML_CD'] == origin) &
        (planned_movements_df['DEST_TRML_CD'] == destination)
    ]['SHPMT_NBR'].unique()

    if len(eligible_shipments) == 0:
        continue

    # B-fix: never assign a shipment to more than one trailer globally
    eligible_shipments = [s for s in eligible_shipments if s not in assigned_shipments]
    if len(eligible_shipments) == 0:
        continue
    num_in_dispatch = np.random.randint(2, 6)
    selected = np.random.choice(
        eligible_shipments,
        min(num_in_dispatch, len(eligible_shipments)),
        replace=False
    )
    assigned_shipments.update(selected)

    # A-fix: ONE date per dispatch, shared by both trailers (a dispatch is one
    # movement event — one driver, one departure)
    if dispatch_id <= 5:
        dispatch_date = last_week_monday + timedelta(days=int(np.random.randint(0, 7)))
    else:
        dispatch_date = (datetime.now() - timedelta(days=int(np.random.randint(0, 56)))).date()

    # Split selected shipments across two trailers on this dispatch
    half = len(selected) // 2 if len(selected) > 1 else 1
    trailer_loads = [selected[:half], selected[half:]]

    for trailer_num, load in enumerate(trailer_loads, start=1):
        if len(load) == 0:
            continue
        dispatches.append({
            'DSPTCH_NBR': f'DISP_{dispatch_id:05d}',
            'TRLR_NBR': f'TRL_{dispatch_id:05d}_{trailer_num}',
            'ORIG_TRML_CD': origin,
            'DEST_TRML_CD': destination,
            'DRVR_ID': f'DRV_{dispatch_id:03d}',
            'LH_DSPTCH_DT': dispatch_date,
            'SHPMT_NBR_LST': ','.join(load)
        })
    dispatch_id += 1

dispatches_df = pd.DataFrame(dispatches)

utilization_records = []

for _, dispatch in dispatches_df.iterrows():
    shipment_ids = [s for s in dispatch['SHPMT_NBR_LST'].split(',') if s]

    total_cube = 0
    total_weight = 0

    for sid in shipment_ids:
        row = shipments[shipments['SHPMT_NBR'] == sid]
        if not row.empty:
            total_cube += row['TOT_CUBE_FT'].values[0]
            total_weight += row['TOT_WGT_LB'].values[0]

    cube_utilization = (total_cube / trailer_capacity_cube) * 100
    weight_utilization = (total_weight / max_weight_lbs) * 100

    # A trailer is FULL when it hits EITHER limit (volume or weight),
    # whichever comes first. Effective utilization is therefore the MAX
    # of the two percentages: a weighed-out trailer at 40% cube is ~full.
    actual_utilization = max(cube_utilization, weight_utilization)

    utilization_records.append({
        'TRLR_NBR': dispatch['TRLR_NBR'],
        'DSPTCH_NBR': dispatch['DSPTCH_NBR'],
        'LH_DSPTCH_DT': dispatch['LH_DSPTCH_DT'],
        'ORIG_TRML_CD': dispatch['ORIG_TRML_CD'],
        'DEST_TRML_CD': dispatch['DEST_TRML_CD'],
        'LD_CUBE_FT': total_cube,
        'LD_WGT_LB': total_weight,
        'TRLR_CAP_CUBE': trailer_capacity_cube,
        'WGT_LMT_LB': max_weight_lbs,
        'UTIL_PCT_1': round(cube_utilization, 2),
        'UTIL_PCT_2': round(weight_utilization, 2),
        'CNSTRNT_CD': 'W' if weight_utilization > cube_utilization else 'C',
        'UTIL_PCT_3': round(actual_utilization, 2),
        'SHPMT_CNT': len(shipment_ids)
    })

# PHYSICAL CAP: no load may exceed trailer cube capacity; scale the load's
# member-shipment cubes proportionally so shipment sums stay consistent.
_disp_members = dict(zip(dispatches_df["TRLR_NBR"], dispatches_df["SHPMT_NBR_LST"]))
shipments["TOT_CUBE_FT"] = shipments["TOT_CUBE_FT"].astype(float)
for _r in utilization_records:
    if _r["LD_CUBE_FT"] > 2000:
        _f = 2000.0 / _r["LD_CUBE_FT"]
        _members = set(str(_disp_members.get(_r["TRLR_NBR"], "")).split(","))
        _members.discard("")
        _r["LD_CUBE_FT"] = 2000
        _r["UTIL_PCT_1"] = 100.0
        _r["UTIL_PCT_3"] = float(max(_r["UTIL_PCT_1"], _r["UTIL_PCT_2"]))
        if _members:
            _mask = shipments["SHPMT_NBR"].isin(_members)
            shipments.loc[_mask, "TOT_CUBE_FT"] = (
                shipments.loc[_mask, "TOT_CUBE_FT"] * _f).round(1)

utilization_df = pd.DataFrame(utilization_records)


# ===============================================================
# LANE REFERENCE (miles, cost, schedules, service standard)
# ===============================================================
lane_rows = []
np.random.seed(77)
_miles_seed = {}
for o in terminals:
    for dd in terminals:
        if o == dd:
            continue
        key = tuple(sorted([o, dd]))
        if key not in _miles_seed:
            _miles_seed[key] = int(np.random.randint(180, 650))
        miles = _miles_seed[key]
        lane_rows.append({
            'ORIG_TRML_CD': o, 'DEST_TRML_CD': dd,
            'LANE_MILES': miles,
            'CPM_USD': round(float(np.random.uniform(1.85, 2.30)), 2),
            'SCHED_PER_WK': int(np.random.randint(3, 8)),
            'SVC_STD_DAYS': 1 if miles < 400 else 2,
        })
lane_ref = pd.DataFrame(lane_rows)
# frequency-rationalization seed: make SGF->MEM a high-frequency lane
lane_ref.loc[(lane_ref['ORIG_TRML_CD'] == 'SGF') &
             (lane_ref['DEST_TRML_CD'] == 'MEM'), 'SCHED_PER_WK'] = 6

# ===============================================================
# SEEDED SCENARIOS (deterministic, for the action layer)
# ===============================================================
def _mk_shipment(sid, o, dd, svc, wgt, cube, dt):
    return {'SHPMT_NBR': sid, 'CUST_CD': f'CUST_{np.random.randint(100,999)}',
            'ORIG_TRML_CD': o, 'DEST_TRML_CD': dd, 'SVC_TYP_CD': svc,
            'TOT_WGT_LB': wgt, 'TOT_CUBE_FT': cube, 'REV_AMT': round(wgt * 0.11, 2),
            'SHPMT_CRT_DT': dt}

def _mk_trailer(tid, did, o, dd, dt, loads):
    cube = sum(c for _, c in loads); wgt = sum(w for w, _ in loads)
    cu = round(cube / 2000 * 100, 1); wu = round(wgt / 20000 * 100, 1)
    return ({'DSPTCH_NBR': did, 'TRLR_NBR': tid, 'ORIG_TRML_CD': o,
             'DEST_TRML_CD': dd, 'DRVR_ID': f'DRV_{np.random.randint(100,999)}',
             'LH_DSPTCH_DT': dt, 'SHPMT_NBR_LST': ''},
            {'TRLR_NBR': tid, 'DSPTCH_NBR': did, 'LH_DSPTCH_DT': dt,
             'ORIG_TRML_CD': o, 'DEST_TRML_CD': dd, 'LD_CUBE_FT': cube,
             'LD_WGT_LB': wgt, 'TRLR_CAP_CUBE': 2000, 'WGT_LMT_LB': 20000,
             'UTIL_PCT_1': cu, 'UTIL_PCT_2': wu,
             'CNSTRNT_CD': 'W' if wu > cu else 'C',
             'UTIL_PCT_3': max(cu, wu), 'SHPMT_CNT': len(loads)})

seed_date = (datetime.now() - timedelta(days=10)).date()
extra_ship, extra_disp, extra_util = [], [], []

# A. ELIGIBLE consolidation pair on HAR->ATL: fits cube+weight, no Priority
pairA = [('TRLR_901', 'DISP_00901', [(5200, 430), (4100, 380)], ['Standard', 'Economy']),
         ('TRLR_902', 'DISP_00902', [(4800, 410), (3900, 350)], ['Economy', 'Standard'])]
sidn = 900
for tid, did, loads, svcs in pairA:
    d, u = _mk_trailer(tid, did, 'HAR', 'ATL', seed_date, loads)
    sids = []
    for (wgt, cube), svc in zip(loads, svcs):
        sid = f'SHIP_{sidn:04d}'; sidn += 1
        extra_ship.append(_mk_shipment(sid, 'HAR', 'ATL', svc, wgt, cube,
                                       (seed_date - timedelta(days=2))))
        sids.append(sid)
    d['SHPMT_NBR_LST'] = ','.join(sids)
    extra_disp.append(d); extra_util.append(u)

# B. TRAP pair on STL->MEM: fits cube+weight BUT TRLR_912 carries a Priority
#    shipment — ineligible under the priority-hold rule (invisible in trlr_util_fct)
pairB = [('TRLR_911', 'DISP_00911', [(5600, 460), (3800, 300)], ['Standard', 'Economy']),
         ('TRLR_912', 'DISP_00912', [(5100, 420), (4400, 360)], ['Priority', 'Standard'])]
for tid, did, loads, svcs in pairB:
    d, u = _mk_trailer(tid, did, 'STL', 'MEM', seed_date, loads)
    sids = []
    for (wgt, cube), svc in zip(loads, svcs):
        sid = f'SHIP_{sidn:04d}'; sidn += 1
        extra_ship.append(_mk_shipment(sid, 'STL', 'MEM', svc, wgt, cube,
                                       (seed_date - timedelta(days=2))))
        sids.append(sid)
    d['SHPMT_NBR_LST'] = ','.join(sids)
    extra_disp.append(d); extra_util.append(u)

# C. Low-fill loads on the high-frequency SGF->MEM lane (frequency candidate)
for i, days_ago in enumerate([9, 16, 23]):
    dt = (datetime.now() - timedelta(days=days_ago)).date()
    tid, did = f'TRLR_92{i}', f'DISP_0092{i}'
    loads, svcs = [(3200, 380), (2400, 310)], ['Economy', 'Economy']
    d, u = _mk_trailer(tid, did, 'SGF', 'MEM', dt, loads)
    sids = []
    for (wgt, cube), svc in zip(loads, svcs):
        sid = f'SHIP_{sidn:04d}'; sidn += 1
        extra_ship.append(_mk_shipment(sid, 'SGF', 'MEM', svc, wgt, cube,
                                       (dt - timedelta(days=2))))
        sids.append(sid)
    d['SHPMT_NBR_LST'] = ','.join(sids)
    extra_disp.append(d); extra_util.append(u)

shipments = pd.concat([shipments, pd.DataFrame(extra_ship)], ignore_index=True)
dispatches_df = pd.concat([dispatches_df, pd.DataFrame(extra_disp)], ignore_index=True)
utilization_df = pd.concat([utilization_df, pd.DataFrame(extra_util)],
                           ignore_index=True)
lane_ref.to_csv('lane_ref.csv', index=False)

shipments.to_csv('shipments.csv', index=False)
# Coverage: every shipment gets at least a direct planned leg (closes the
# seeded-scenario gap flagged in review).
_pm_have = set(planned_movements_df["SHPMT_NBR"])
_extra = []
for _, _s in shipments.iterrows():
    if _s["SHPMT_NBR"] not in _pm_have:
        _extra.append({"MVMT_NBR": f"MVMT_S{len(_extra):04d}",
                       "SHPMT_NBR": _s["SHPMT_NBR"], "LEG_SEQ_NBR": 1,
                       "ORIG_TRML_CD": _s["ORIG_TRML_CD"],
                       "DEST_TRML_CD": _s["DEST_TRML_CD"],
                       "SVC_TYP_CD": _s.get("SVC_TYP_CD", "STD")})
if _extra:
    planned_movements_df = pd.concat(
        [planned_movements_df, pd.DataFrame(_extra)], ignore_index=True)
planned_movements_df.to_csv('planned_movements.csv', index=False)
dispatches_df.to_csv('dispatches.csv', index=False)
utilization_df.to_csv('cube_utilization.csv', index=False)

print("Data generation complete!")
print(f"Shipments: {len(shipments)} records")
print(f"Planned Movements: {len(planned_movements_df)} records")
print(f"Dispatches (trailer rows): {len(dispatches_df)} records")
print(f"Cube Utilization: {len(utilization_df)} records")
