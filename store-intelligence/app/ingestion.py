import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from database import get_db
from models import IngestEventResult, IngestResponse, StoreEvent
logger = logging.getLogger(__name__)

def ingest_events(events: List[StoreEvent], trace_id: str) -> IngestResponse:
    results: List[IngestEventResult] = []
    accepted = 0
    duplicates = 0
    invalid = 0
    if not events:
        return IngestResponse(accepted=0, duplicates=0, invalid=0, results=[], trace_id=trace_id)
    event_ids = [e.event_id for e in events]
    existing_ids = _get_existing_event_ids(event_ids)
    to_insert = []
    session_updates = {}
    for event in events:
        if event.event_id in existing_ids:
            results.append(IngestEventResult(event_id=event.event_id, status='duplicate', error='event_id already exists'))
            duplicates += 1
            continue
        validation_error = _validate_business_rules(event)
        if validation_error:
            results.append(IngestEventResult(event_id=event.event_id, status='invalid', error=validation_error))
            invalid += 1
            continue
        to_insert.append(event)
        results.append(IngestEventResult(event_id=event.event_id, status='accepted'))
        accepted += 1
        vid = event.visitor_id
        if vid not in session_updates:
            session_updates[vid] = []
        session_updates[vid].append(event)
    if to_insert:
        _bulk_insert_events(to_insert)
        for visitor_id, visitor_events in session_updates.items():
            _update_session(visitor_id, visitor_events)
        _correlate_pos_transactions(to_insert)
    logger.info(f'[{trace_id}] Ingest complete: accepted={accepted}, duplicates={duplicates}, invalid={invalid}')
    return IngestResponse(accepted=accepted, duplicates=duplicates, invalid=invalid, results=results, trace_id=trace_id)

def _validate_business_rules(event: StoreEvent) -> Optional[str]:
    if event.event_type in ('ENTRY', 'EXIT', 'REENTRY') and event.zone_id:
        pass
    if event.event_type in ('ZONE_ENTER', 'ZONE_EXIT', 'ZONE_DWELL'):
        if not event.zone_id:
            return f'zone_id required for event_type {event.event_type}'
    if event.event_type == 'BILLING_QUEUE_JOIN':
        if event.metadata.queue_depth is None:
            pass
    return None

def _get_existing_event_ids(event_ids: List[str]) -> set:
    if not event_ids:
        return set()
    try:
        with get_db() as conn:
            placeholders = ','.join('?' * len(event_ids))
            rows = conn.execute(f'SELECT event_id FROM events WHERE event_id IN ({placeholders})', event_ids).fetchall()
            return {row['event_id'] for row in rows}
    except Exception as e:
        logger.error(f'Failed to check existing event IDs: {e}')
        return set()

def _bulk_insert_events(events: List[StoreEvent]) -> None:
    rows = []
    for e in events:
        rows.append((e.event_id, e.store_id, e.camera_id, e.visitor_id, e.event_type, e.timestamp, e.zone_id, e.dwell_ms, 1 if e.is_staff else 0, e.confidence, e.metadata.queue_depth, e.metadata.sku_zone, e.metadata.session_seq, json.dumps(e.model_dump())))
    try:
        with get_db() as conn:
            conn.executemany('\n                INSERT OR IGNORE INTO events\n                (event_id, store_id, camera_id, visitor_id, event_type,\n                 timestamp, zone_id, dwell_ms, is_staff, confidence,\n                 queue_depth, sku_zone, session_seq, raw_json)\n                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)\n            ', rows)
    except Exception as e:
        logger.error(f'Bulk insert failed: {e}')
        raise

def _update_session(visitor_id: str, new_events: List[StoreEvent]) -> None:
    try:
        with get_db() as conn:
            for event in new_events:
                if event.is_staff:
                    continue
                if event.event_type == 'ENTRY':
                    session_id = str(uuid.uuid4())
                    conn.execute('\n                        INSERT OR REPLACE INTO sessions\n                        (session_id, store_id, visitor_id, entry_time, is_staff,\n                         reentry_count, zones_visited, reached_billing, did_purchase, total_dwell_ms)\n                        VALUES (?,?,?,?,?,?,?,?,?,?)\n                    ', (session_id, event.store_id, visitor_id, event.timestamp, 0, 0, json.dumps([]), 0, 0, 0))
                elif event.event_type == 'REENTRY':
                    session_id = str(uuid.uuid4())
                    conn.execute('\n                        INSERT INTO sessions\n                        (session_id, store_id, visitor_id, entry_time, is_staff,\n                         reentry_count, zones_visited, reached_billing, did_purchase, total_dwell_ms)\n                        VALUES (?,?,?,?,?,?,?,?,?,?)\n                    ', (session_id, event.store_id, visitor_id, event.timestamp, 0, 1, json.dumps([]), 0, 0, 0))
                elif event.event_type == 'EXIT':
                    conn.execute("\n                        UPDATE sessions SET exit_time = ?, updated_at = datetime('now')\n                        WHERE session_id = (\n                            SELECT session_id FROM sessions\n                            WHERE visitor_id = ? AND store_id = ? AND exit_time IS NULL\n                            ORDER BY entry_time DESC LIMIT 1\n                        )\n                    ", (event.timestamp, visitor_id, event.store_id))
                elif event.event_type in ('ZONE_ENTER', 'ZONE_DWELL'):
                    row = conn.execute('\n                        SELECT session_id, zones_visited, total_dwell_ms\n                        FROM sessions\n                        WHERE visitor_id = ? AND store_id = ? AND exit_time IS NULL\n                        ORDER BY entry_time DESC LIMIT 1\n                    ', (visitor_id, event.store_id)).fetchone()
                    if row:
                        zones = json.loads(row['zones_visited'] or '[]')
                        if event.zone_id and event.zone_id not in zones:
                            zones.append(event.zone_id)
                        total_dwell = row['total_dwell_ms'] + event.dwell_ms
                        reached_billing = 1 if event.zone_id in ('BILLING', 'BILLING_QUEUE') else 0
                        conn.execute("\n                            UPDATE sessions\n                            SET zones_visited = ?,\n                                total_dwell_ms = ?,\n                                reached_billing = MAX(reached_billing, ?),\n                                updated_at = datetime('now')\n                            WHERE session_id = ?\n                        ", (json.dumps(zones), total_dwell, reached_billing, row['session_id']))
    except Exception as e:
        logger.error(f'Session update failed for {visitor_id}: {e}')

def _correlate_pos_transactions(events: List[StoreEvent]) -> None:
    billing_events = [e for e in events if e.event_type in ('BILLING_QUEUE_JOIN', 'ZONE_ENTER') and e.zone_id in ('BILLING', 'BILLING_QUEUE') and (not e.is_staff)]
    if not billing_events:
        return
    try:
        with get_db() as conn:
            pos_rows = conn.execute('\n                SELECT transaction_id, store_id, timestamp, basket_value_inr\n                FROM pos_transactions\n                WHERE correlated_visitor_id IS NULL\n            ').fetchall()
            for txn in pos_rows:
                txn_ts = datetime.fromisoformat(txn['timestamp'].replace('Z', '+00:00'))
                best_match = None
                best_diff = float('inf')
                for event in billing_events:
                    if event.store_id != txn['store_id']:
                        continue
                    event_ts = datetime.fromisoformat(event.timestamp.replace('Z', '+00:00'))
                    diff = (txn_ts - event_ts).total_seconds()
                    if 0 <= diff <= 300 and diff < best_diff:
                        best_diff = diff
                        best_match = event.visitor_id
                if best_match:
                    conn.execute('\n                        UPDATE pos_transactions\n                        SET correlated_visitor_id = ?\n                        WHERE transaction_id = ?\n                    ', (best_match, txn['transaction_id']))
                    conn.execute("\n                        UPDATE sessions SET did_purchase = 1, updated_at = datetime('now')\n                        WHERE visitor_id = ? AND store_id = ?\n                        AND exit_time IS NULL OR exit_time > ?\n                    ", (best_match, txn['store_id'], txn['timestamp']))
    except Exception as e:
        logger.error(f'POS correlation failed: {e}')

def ingest_pos_transactions(transactions: List[Dict]) -> int:
    rows = []
    for txn in transactions:
        rows.append((txn['transaction_id'], txn['store_id'], txn['timestamp'], txn['basket_value_inr']))
    try:
        with get_db() as conn:
            conn.executemany('\n                INSERT OR IGNORE INTO pos_transactions\n                (transaction_id, store_id, timestamp, basket_value_inr)\n                VALUES (?,?,?,?)\n            ', rows)
        return len(rows)
    except Exception as e:
        logger.error(f'POS ingest failed: {e}')
        return 0