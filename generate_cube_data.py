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

utilization_df = pd.DataFrame(utilization_records)

shipments.to_csv('shipments.csv', index=False)
planned_movements_df.to_csv('planned_movements.csv', index=False)
dispatches_df.to_csv('dispatches.csv', index=False)
utilization_df.to_csv('cube_utilization.csv', index=False)

print("Data generation complete!")
print(f"Shipments: {len(shipments)} records")
print(f"Planned Movements: {len(planned_movements_df)} records")
print(f"Dispatches (trailer rows): {len(dispatches_df)} records")
print(f"Cube Utilization: {len(utilization_df)} records")
