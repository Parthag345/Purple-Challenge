import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple
from database import get_db
from models import StoreMetrics, ZoneDwellMetric
logger = logging.getLogger(__name__)

def get_store_metrics(store_id: str) -> Optional[StoreMetrics]:
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    try:
        with get_db() as conn:
            visitors_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as count\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type = 'ENTRY'\n            ", (store_id,)).fetchone()
            unique_visitors = visitors_row['count'] if visitors_row else 0
            conversions_row = conn.execute('\n                SELECT COUNT(DISTINCT s.visitor_id) as converted_count\n                FROM sessions s\n                WHERE s.store_id = ?\n                  AND s.is_staff = 0\n                  AND s.did_purchase = 1\n            ', (store_id,)).fetchone()
            converted_visitors = conversions_row['converted_count'] if conversions_row else 0
            if converted_visitors == 0:
                pos_row = conn.execute('\n                    SELECT COUNT(DISTINCT correlated_visitor_id) as count\n                    FROM pos_transactions\n                    WHERE store_id = ? AND correlated_visitor_id IS NOT NULL\n                ', (store_id,)).fetchone()
                if pos_row:
                    converted_visitors = pos_row['count']
            conversion_rate = converted_visitors / unique_visitors if unique_visitors > 0 else 0.0
            dwell_row = conn.execute('\n                SELECT AVG(total_dwell_ms) as avg_dwell\n                FROM sessions\n                WHERE store_id = ? AND is_staff = 0 AND total_dwell_ms > 0\n            ', (store_id,)).fetchone()
            avg_dwell_ms = dwell_row['avg_dwell'] if dwell_row and dwell_row['avg_dwell'] else 0.0
            avg_dwell_s = avg_dwell_ms / 1000.0
            if avg_dwell_s == 0:
                dwell_fallback = conn.execute("\n                    SELECT AVG(dwell_ms) as avg_dwell\n                    FROM events\n                    WHERE store_id = ? AND is_staff = 0\n                      AND event_type = 'ZONE_DWELL' AND dwell_ms > 0\n                ", (store_id,)).fetchone()
                if dwell_fallback and dwell_fallback['avg_dwell']:
                    avg_dwell_s = dwell_fallback['avg_dwell'] / 1000.0
            zone_rows = conn.execute("\n                SELECT\n                    zone_id,\n                    COUNT(*) as visit_count,\n                    AVG(CASE WHEN dwell_ms > 0 THEN dwell_ms ELSE NULL END) as avg_dwell_ms\n                FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')\n                  AND zone_id IS NOT NULL\n                GROUP BY zone_id\n                ORDER BY visit_count DESC\n            ", (store_id,)).fetchall()
            occupancy_rows = conn.execute("\n                SELECT zone_id, COUNT(DISTINCT visitor_id) as occupancy\n                FROM events e1\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type = 'ZONE_ENTER'\n                  AND zone_id IS NOT NULL\n                  AND NOT EXISTS (\n                      SELECT 1 FROM events e2\n                      WHERE e2.visitor_id = e1.visitor_id\n                        AND e2.store_id = e1.store_id\n                        AND e2.event_type = 'ZONE_EXIT'\n                        AND e2.zone_id = e1.zone_id\n                        AND e2.timestamp > e1.timestamp\n                  )\n                GROUP BY zone_id\n            ", (store_id,)).fetchall()
            occupancy_map = {r['zone_id']: r['occupancy'] for r in occupancy_rows}
            zone_metrics = []
            for row in zone_rows:
                if not row['zone_id']:
                    continue
                zone_metrics.append(ZoneDwellMetric(zone_id=row['zone_id'], avg_dwell_seconds=round((row['avg_dwell_ms'] or 0) / 1000.0, 1), visit_count=row['visit_count'], current_occupancy=occupancy_map.get(row['zone_id'], 0)))
            queue_row = conn.execute("\n                SELECT MAX(queue_depth) as max_depth\n                FROM events\n                WHERE store_id = ?\n                  AND event_type = 'BILLING_QUEUE_JOIN'\n                  AND timestamp > datetime('now', '-10 minutes')\n            ", (store_id,)).fetchone()
            current_queue_depth = int(queue_row['max_depth'] or 0) if queue_row else 0
            queue_joins = conn.execute("\n                SELECT COUNT(*) as cnt FROM events\n                WHERE store_id = ? AND is_staff = 0\n                  AND event_type = 'BILLING_QUEUE_JOIN'\n            ", (store_id,)).fetchone()
            queue_abandons = conn.execute("\n                SELECT COUNT(*) as cnt FROM events\n                WHERE store_id = ? AND is_staff = 0\n                  AND event_type = 'BILLING_QUEUE_ABANDON'\n            ", (store_id,)).fetchone()
            total_joins = queue_joins['cnt'] if queue_joins else 0
            total_abandons = queue_abandons['cnt'] if queue_abandons else 0
            abandonment_rate = total_abandons / total_joins if total_joins > 0 else 0.0
            pos_row = conn.execute('\n                SELECT COUNT(*) as txn_count, SUM(basket_value_inr) as total_revenue\n                FROM pos_transactions WHERE store_id = ?\n            ', (store_id,)).fetchone()
            total_transactions = pos_row['txn_count'] if pos_row else 0
            total_revenue = pos_row['total_revenue'] if pos_row and pos_row['total_revenue'] else 0.0
            data_confidence = 'HIGH' if unique_visitors >= 20 else 'MEDIUM' if unique_visitors >= 5 else 'LOW'
            return StoreMetrics(store_id=store_id, as_of=datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), unique_visitors=unique_visitors, conversion_rate=round(conversion_rate, 4), avg_dwell_seconds=round(avg_dwell_s, 1), zone_metrics=zone_metrics, current_queue_depth=current_queue_depth, abandonment_rate=round(abandonment_rate, 4), total_transactions=total_transactions, total_revenue_inr=round(total_revenue, 2), data_confidence=data_confidence)
    except Exception as e:
        logger.error(f'Metrics computation failed for {store_id}: {e}')
        raise

def get_historical_conversion_rate(store_id: str, days: int=7) -> Optional[float]:
    try:
        with get_db() as conn:
            row = conn.execute("\n                SELECT AVG(conversion_rate) as avg_rate\n                FROM metric_snapshots\n                WHERE store_id = ? AND snapshot_date >= date('now', ? || ' days')\n            ", (store_id, f'-{days}')).fetchone()
            return float(row['avg_rate']) if row and row['avg_rate'] else None
    except Exception:
        return None

def get_last_event_timestamp(store_id: str) -> Optional[str]:
    try:
        with get_db() as conn:
            row = conn.execute('\n                SELECT MAX(timestamp) as last_ts\n                FROM events WHERE store_id = ?\n            ', (store_id,)).fetchone()
            return row['last_ts'] if row else None
    except Exception:
        return None

def get_active_visitor_count(store_id: str) -> int:
    try:
        with get_db() as conn:
            row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as cnt\n                FROM events e1\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type = 'ENTRY'\n                  AND NOT EXISTS (\n                      SELECT 1 FROM events e2\n                      WHERE e2.visitor_id = e1.visitor_id\n                        AND e2.store_id = e1.store_id\n                        AND e2.event_type = 'EXIT'\n                        AND e2.timestamp > e1.timestamp\n                  )\n            ", (store_id,)).fetchone()
            return row['cnt'] if row else 0
    except Exception:
        return 0

def get_events_last_hour(store_id: str) -> int:
    try:
        with get_db() as conn:
            row = conn.execute("\n                SELECT COUNT(*) as cnt FROM events\n                WHERE store_id = ?\n                  AND ingested_at > datetime('now', '-1 hour')\n            ", (store_id,)).fetchone()
            return row['cnt'] if row else 0
    except Exception:
        return 0