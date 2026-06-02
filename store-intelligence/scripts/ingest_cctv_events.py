import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path
ROOT = Path(__file__).parent.parent
DEFAULT_EVENTS = ROOT / 'data' / 'brigade_cctv_events.jsonl'

def post_batch(events: list, api_url: str, batch_size: int=250) -> dict:
    total = {'accepted': 0, 'duplicates': 0, 'rejected': 0, 'batches': 0}
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        payload = json.dumps({'events': batch}).encode()
        req = urllib.request.Request(f'{api_url}/events/ingest', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                total['accepted'] += result.get('accepted', 0)
                total['duplicates'] += result.get('duplicates', 0)
                total['rejected'] += result.get('rejected', 0)
                total['batches'] += 1
                print(f"  Batch {total['batches']}: accepted={result.get('accepted', 0)}, dup={result.get('duplicates', 0)}, rejected={result.get('rejected', 0)}")
        except urllib.error.URLError as e:
            print(f'  ERROR: {e}')
            break
    return total

def main():
    parser = argparse.ArgumentParser(description='Ingest CCTV events into Store Intelligence API')
    parser.add_argument('--api-url', default='http://localhost:8000')
    parser.add_argument('--events-file', default=str(DEFAULT_EVENTS))
    parser.add_argument('--batch-size', type=int, default=250)
    args = parser.parse_args()
    events_path = Path(args.events_file)
    if not events_path.exists():
        print(f'ERROR: Events file not found: {events_path}')
        print(f'Run first: python scripts/run_cctv_pipeline.py')
        sys.exit(1)
    try:
        with urllib.request.urlopen(f'{args.api_url}/health', timeout=5) as resp:
            health = json.loads(resp.read())
            print(f"API: {args.api_url} [{health.get('status', 'unknown')}]")
    except Exception as e:
        print(f'API not reachable at {args.api_url}: {e}')
        print('Start API with: python app/main.py')
        sys.exit(1)
    events = []
    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))
    print(f'\nLoaded {len(events)} events from {events_path.name}')
    from collections import Counter
    type_counts = Counter((e['event_type'] for e in events))
    for et, cnt in sorted(type_counts.items()):
        print(f'  {et}: {cnt}')
    print(f'\nIngesting to {args.api_url}/events/ingest ...')
    summary = post_batch(events, args.api_url, batch_size=args.batch_size)
    print(f'\nDone!')
    print(f"  Total accepted:   {summary['accepted']}")
    print(f"  Total duplicates: {summary['duplicates']}")
    print(f"  Total rejected:   {summary['rejected']}")
    print(f"  Batches:          {summary['batches']}")
    try:
        with urllib.request.urlopen(f'{args.api_url}/stores/STORE_BLR_002/metrics', timeout=10) as resp:
            m = json.loads(resp.read())
            print(f'\nUpdated STORE_BLR_002 metrics:')
            print(f"  unique_visitors:  {m.get('unique_visitors', 0)}")
            print(f"  conversion_rate:  {m.get('conversion_rate', 0):.1%}")
            print(f"  avg_dwell_secs:   {m.get('avg_dwell_seconds', 0):.0f}s")
            print(f"  total_revenue:    INR {m.get('total_revenue_inr', 0):,.2f}")
            print(f"  data_confidence:  {m.get('data_confidence', 'LOW')}")
    except Exception as e:
        print(f'Could not fetch updated metrics: {e}')
if __name__ == '__main__':
    main()