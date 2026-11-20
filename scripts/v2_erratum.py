"""v2 erratum: DST-aware local-midnight window vs NWS LST (fixed standard offset) window."""
import collections
import json
import sys
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, '.')
from forecast_lab import config, weather
from forecast_lab.dataset import Sources

cfg = config.load('configs/nyc-0800-v2.toml')
src = Sources(cfg, offline=True)
STD = timedelta(hours=-5)  # America/New_York standard offset (Jan 1)
def split_of(d):
    s = cfg['split']
    if d < date.fromisoformat(s['test_start']):
        return 'dev'
    if d <= date.fromisoformat(s['test_end']):
        return 'test'
    return 'audit'
cov = {r['event_id']: r for r in map(json.loads, open('data/work-v2/dataset/coverage.jsonl')) if r['event_id']}
cnt = collections.Counter()
examples = collections.defaultdict(list)
for line in open('data/work-v2/dataset/features.jsonl'):
    r = json.loads(line)
    d = date.fromisoformat(r['event_date'])
    sp = split_of(d)
    scored = cov[r['event_id']]['primary_reason'] is None
    lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc) - STD
    hi = lo + timedelta(days=1)
    dst = lo != datetime.fromisoformat(r['weather']['obs']['window_utc'][0].replace('Z', '+00:00')) if r['weather'].get('obs') else None
    cnt[(sp, 'events', scored)] += 1
    if dst:
        cnt[(sp, 'dst_events', scored)] += 1
    sel = r['weather'].get('selected')
    if sel:
        runs, _ = src.mos_runs(d)
        rt = datetime.fromisoformat(sel['runtime_utc'].replace('Z', '+00:00'))
        samples = [x['tmp'] for x in runs[rt] if lo <= x['ftime'] < hi and x['tmp'] is not None]
        new = max(samples) if len(samples) >= 7 else None
        if new != sel.get('sampled_max_local_f'):
            cnt[(sp, 'sampled_changed', scored)] += 1
            examples['sampled'].append((r['event_id'], sel.get('sampled_max_local_f'), new))
    ob = r['weather'].get('obs')
    if ob:
        cut = datetime.fromisoformat(r['cutoff_utc'].replace('Z', '+00:00'))
        o = weather.observation_summary(src.asos(d.year), d, cut, ob['latency_min'])
        assert o['obs_max_so_far_f'] == ob['obs_max_so_far_f'] and o['n_obs'] == ob['n_obs'], r['event_id']
        sel_obs = [t for v, t in src.asos(d.year) if lo <= v <= cut - timedelta(minutes=ob['latency_min'])]
        new = max(sel_obs) if sel_obs else None
        if new != ob['obs_max_so_far_f']:
            cnt[(sp, 'obs_max_changed', scored)] += 1
            examples['obs'].append((r['event_id'], ob['obs_max_so_far_f'], new))
        if len(sel_obs) != ob['n_obs']:
            cnt[(sp, 'n_obs_changed', scored)] += 1
for k in sorted(cnt):
    print(k, cnt[k])
print('sampled examples', examples['sampled'][:5])
print('obs examples', examples['obs'][:8])
json.dump({'counts': {'|'.join(map(str, k)): v for k, v in cnt.items()}, 'examples': examples}, open('evidence/v3/v2-erratum.json', 'w'), indent=1)
