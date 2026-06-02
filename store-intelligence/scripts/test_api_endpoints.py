import urllib.request, json

def get(path):
    with urllib.request.urlopen(f'http://localhost:8000{path}', timeout=5) as r:
        return json.loads(r.read())
h = get('/health')
print('=== /health ===')
status = h['status']
version = h['version']
stores = len(h['stores'])
print(f'status: {status}, version: {version}, stores: {stores}')
for s in h['stores']:
    print(f"  store {s['store_id']}: {s['feed_status']}, events_1h={s['events_last_hour']}, active={s['active_visitors']}")
m = get('/stores/STORE_BLR_002/metrics')
print()
print('=== /stores/STORE_BLR_002/metrics ===')
for k, v in m.items():
    print(f'  {k}: {v}')
f = get('/stores/STORE_BLR_002/funnel')
print()
print('=== /stores/STORE_BLR_002/funnel ===')
for s in f['stages']:
    stage = s['stage']
    count = s['count']
    drop = s['drop_off_pct']
    print(f'  {stage}: count={count}, drop_off={drop}%')
hm = get('/stores/STORE_BLR_002/heatmap')
print()
print('=== /stores/STORE_BLR_002/heatmap ===')
for cell in hm['cells']:
    zid = cell['zone_id']
    score = cell['normalised_score']
    visits = cell['visit_frequency']
    print(f'  {zid}: normalised_score={score}, visits={visits}')
a = get('/stores/STORE_BLR_002/anomalies')
anomaly_count = len(a['anomalies'])
print()
print(f'=== /stores/STORE_BLR_002/anomalies === ({anomaly_count} anomalies)')
for an in a['anomalies']:
    at = an['anomaly_type']
    sev = an['severity']
    desc = an['description'][:80]
    print(f'  {at} [{sev}]: {desc}')