import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional
from database import get_db
from models import FunnelResponse, FunnelStage
logger = logging.getLogger(__name__)

def get_funnel(store_id: str, window_hours: int=24) -> FunnelResponse:
    try:
        with get_db() as conn:
            latest_row = conn.execute('SELECT MAX(timestamp) as latest FROM events WHERE store_id = ?', (store_id,)).fetchone()
            if latest_row and latest_row['latest']:
                latest_dt = datetime.fromisoformat(latest_row['latest'].replace('Z', '+00:00'))
            else:
                latest_dt = datetime.now(timezone.utc)
    except Exception:
        latest_dt = datetime.now(timezone.utc)
    window_start_dt = (latest_dt - timedelta(hours=window_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        with get_db() as conn:
            total_entry_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('ENTRY', 'REENTRY')\n                  AND timestamp > ?\n            ", (store_id, window_start_dt)).fetchone()
            total_entries = total_entry_row['cnt'] if total_entry_row else 0
            unique_visitors_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type = 'ENTRY'\n                  AND timestamp > ?\n            ", (store_id, window_start_dt)).fetchone()
            unique_visitors = unique_visitors_row['cnt'] if unique_visitors_row else 0
            zone_visitors_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')\n                  AND zone_id NOT IN ('BILLING', 'BILLING_QUEUE')\n                  AND zone_id IS NOT NULL\n                  AND timestamp > ?\n            ", (store_id, window_start_dt)).fetchone()
            zone_visitors = zone_visitors_row['cnt'] if zone_visitors_row else 0
            billing_visitors_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('BILLING_QUEUE_JOIN', 'ZONE_ENTER')\n                  AND zone_id IN ('BILLING', 'BILLING_QUEUE')\n                  AND timestamp > ?\n            ", (store_id, window_start_dt)).fetchone()
            billing_visitors = billing_visitors_row['cnt'] if billing_visitors_row else 0
            purchase_visitors_row = conn.execute('\n                SELECT COUNT(DISTINCT s.visitor_id) as cnt\n                FROM sessions s\n                WHERE s.store_id = ?\n                  AND s.is_staff = 0\n                  AND s.did_purchase = 1\n                  AND s.entry_time > ?\n            ', (store_id, window_start_dt)).fetchone()
            purchase_visitors = purchase_visitors_row['cnt'] if purchase_visitors_row else 0
            if purchase_visitors == 0:
                txn_row = conn.execute('\n                    SELECT COUNT(DISTINCT correlated_visitor_id) as cnt\n                    FROM pos_transactions\n                    WHERE store_id = ?\n                      AND correlated_visitor_id IS NOT NULL\n                      AND timestamp > ?\n                ', (store_id, window_start_dt)).fetchone()
                if txn_row:
                    purchase_visitors = txn_row['cnt']
    except Exception as e:
        logger.error(f'Funnel computation failed for {store_id}: {e}')
        raise

    def drop_off(current: int, previous: int) -> float:
        if previous == 0:
            return 0.0
        return round((1 - current / previous) * 100, 1)
    zone_visitors = min(zone_visitors, unique_visitors)
    billing_visitors = min(billing_visitors, zone_visitors if zone_visitors > 0 else billing_visitors)
    purchase_visitors = min(purchase_visitors, billing_visitors if billing_visitors > 0 else purchase_visitors)
    stages = [FunnelStage(stage='Entry', count=unique_visitors, drop_off_pct=0.0), FunnelStage(stage='Zone Visit', count=zone_visitors, drop_off_pct=drop_off(zone_visitors, unique_visitors)), FunnelStage(stage='Billing Queue', count=billing_visitors, drop_off_pct=drop_off(billing_visitors, zone_visitors)), FunnelStage(stage='Purchase', count=purchase_visitors, drop_off_pct=drop_off(purchase_visitors, billing_visitors))]
    overall_rate = round(purchase_visitors / unique_visitors, 4) if unique_visitors > 0 else 0.0
    window_start_str = _hours_ago_str(window_hours)
    window_end_str = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return FunnelResponse(store_id=store_id, window_start=window_start_str, window_end=window_end_str, stages=stages, total_entries=total_entries, conversions=purchase_visitors, overall_conversion_rate=overall_rate)

def _hours_ago_str(hours: int) -> str:
    from datetime import timedelta
    ts = datetime.now(timezone.utc) - timedelta(hours=hours)
    return ts.strftime('%Y-%m-%dT%H:%M:%SZ')

def get_heatmap(store_id: str) -> Dict:
    try:
        with get_db() as conn:
            rows = conn.execute("\n                SELECT\n                    zone_id,\n                    COUNT(*) as visit_freq,\n                    AVG(CASE WHEN dwell_ms > 0 THEN dwell_ms ELSE NULL END) as avg_dwell_ms\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')\n                  AND zone_id IS NOT NULL\n                  AND zone_id NOT IN ('BILLING', 'BILLING_QUEUE')\n                GROUP BY zone_id\n                ORDER BY visit_freq DESC\n            ", (store_id,)).fetchall()
            session_count_row = conn.execute('\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM sessions WHERE store_id = ? AND is_staff = 0\n            ', (store_id,)).fetchone()
            session_count = session_count_row['cnt'] if session_count_row else 0
            if not rows:
                return {'store_id': store_id, 'as_of': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'cells': []}
            max_freq = max((r['visit_freq'] for r in rows)) or 1
            cells = []
            for row in rows:
                if not row['zone_id']:
                    continue
                norm = round(row['visit_freq'] / max_freq * 100, 1)
                cells.append({'zone_id': row['zone_id'], 'visit_frequency': row['visit_freq'], 'avg_dwell_seconds': round((row['avg_dwell_ms'] or 0) / 1000.0, 1), 'normalised_score': norm, 'data_confidence': session_count >= 20})
            return {'store_id': store_id, 'as_of': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'cells': cells}
    except Exception as e:
        logger.error(f'Heatmap failed for {store_id}: {e}')
        raise