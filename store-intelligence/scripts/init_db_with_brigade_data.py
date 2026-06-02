import sys
import json
import os
import logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('init')
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'app'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'pipeline'))
import database
DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'store_intelligence.db')
database.DB_PATH = DB_PATH
database.init_db()
logger.info(f'Database initialized at {DB_PATH}')
from detect import load_brigade_data
from ingestion import ingest_pos_transactions, ingest_events
from models import StoreEvent
brigade_csv_paths = [os.path.join(os.path.dirname(__file__), '..', '..', 'uploads', 'Brigade_Bangalore_10_April_26 (1)bc6219c.csv'), os.path.join(os.path.dirname(__file__), '..', 'data', 'brigade_pos.csv')]
brigade_loaded = False
for csv_path in brigade_csv_paths:
    if os.path.exists(csv_path):
        txns = load_brigade_data(csv_path)
        n = ingest_pos_transactions(txns)
        total_rev = sum((t['basket_value_inr'] for t in txns))
        logger.info(f'Loaded {n} Brigade POS transactions (INR {total_rev:.2f} total revenue)')
        brigade_loaded = True
        break
if not brigade_loaded:
    logger.warning('Brigade POS CSV not found — loading sample data only')
events_path = os.path.join(os.path.dirname(__file__), '..', 'data', 'sample_events.jsonl')
if os.path.exists(events_path):
    with open(events_path) as f:
        raw_events = [json.loads(line) for line in f if line.strip()]
    logger.info(f'Loading {len(raw_events)} simulation events...')
    accepted_total = 0
    for i in range(0, len(raw_events), 500):
        batch = raw_events[i:i + 500]
        store_events = [StoreEvent(**e) for e in batch]
        result = ingest_events(store_events, trace_id='init')
        accepted_total += result.accepted
        logger.info(f'  Batch {i // 500 + 1}: accepted={result.accepted}, duplicates={result.duplicates}, invalid={result.invalid}')
    logger.info(f'Total accepted: {accepted_total} events')
else:
    logger.warning(f'No events file at {events_path}. Run: python pipeline/detect.py --simulate')
from metrics import get_store_metrics
metrics = get_store_metrics('STORE_BLR_002')
if metrics:
    logger.info(f'STORE_BLR_002 Metrics:')
    logger.info(f'  unique_visitors: {metrics.unique_visitors}')
    logger.info(f'  conversion_rate: {metrics.conversion_rate}')
    logger.info(f'  avg_dwell_seconds: {metrics.avg_dwell_seconds}')
    logger.info(f'  total_transactions: {metrics.total_transactions}')
    logger.info(f'  total_revenue_inr: INR {metrics.total_revenue_inr:.2f}')
    logger.info(f'  data_confidence: {metrics.data_confidence}')
else:
    logger.warning('No metrics returned — check events were ingested')
print('\nDatabase ready. Start API with: python app/main.py')