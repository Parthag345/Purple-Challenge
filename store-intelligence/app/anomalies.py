import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional
from database import get_db
from metrics import get_historical_conversion_rate, get_last_event_timestamp
from models import Anomaly, AnomaliesResponse, AnomalySeverity
logger = logging.getLogger(__name__)
QUEUE_SPIKE_THRESHOLD = 5
CONVERSION_DROP_THRESHOLD = 0.2
DEAD_ZONE_MINUTES = 30
STALE_FEED_MINUTES = 10
CROWD_BUILD_THRESHOLD = 8
PRODUCT_ZONES = ['SKINCARE', 'MAKEUP', 'HAIRCARE', 'FRAGRANCE', 'PERSONAL_CARE']

def get_anomalies(store_id: str) -> AnomaliesResponse:
    anomalies: List[Anomaly] = []
    now = datetime.now(timezone.utc)
    try:
        queue_anomalies = _detect_queue_spike(store_id, now)
        anomalies.extend(queue_anomalies)
        conv_anomalies = _detect_conversion_drop(store_id, now)
        anomalies.extend(conv_anomalies)
        dead_zone_anomalies = _detect_dead_zones(store_id, now)
        anomalies.extend(dead_zone_anomalies)
        stale_anomalies = _detect_stale_feed(store_id, now)
        anomalies.extend(stale_anomalies)
        crowd_anomalies = _detect_crowd_buildup(store_id, now)
        anomalies.extend(crowd_anomalies)
        _persist_anomalies(anomalies)
    except Exception as e:
        logger.error(f'Anomaly detection failed for {store_id}: {e}')
    return AnomaliesResponse(store_id=store_id, active_anomalies=anomalies, as_of=now.strftime('%Y-%m-%dT%H:%M:%SZ'))

def _detect_queue_spike(store_id: str, now: datetime) -> List[Anomaly]:
    anomalies = []
    try:
        with get_db() as conn:
            row = conn.execute("\n                SELECT MAX(queue_depth) as max_depth\n                FROM events\n                WHERE store_id = ?\n                  AND event_type = 'BILLING_QUEUE_JOIN'\n                  AND timestamp > datetime('now', '-5 minutes')\n            ", (store_id,)).fetchone()
            if not row or row['max_depth'] is None:
                return []
            depth = row['max_depth']
            if depth >= QUEUE_SPIKE_THRESHOLD:
                severity = AnomalySeverity.CRITICAL if depth >= 8 else AnomalySeverity.WARN
                anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='BILLING_QUEUE_SPIKE', severity=severity, store_id=store_id, zone_id='BILLING_QUEUE', detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description=f'Billing queue depth is {depth} — threshold is {QUEUE_SPIKE_THRESHOLD}.', suggested_action='Open additional billing counter. Alert store manager immediately.' if severity == AnomalySeverity.CRITICAL else 'Monitor queue. Consider opening second billing station.', metric_value=float(depth), threshold_value=float(QUEUE_SPIKE_THRESHOLD)))
    except Exception as e:
        logger.error(f'Queue spike detection error: {e}')
    return anomalies

def _detect_conversion_drop(store_id: str, now: datetime) -> List[Anomaly]:
    anomalies = []
    try:
        with get_db() as conn:
            total_row = conn.execute("\n                SELECT COUNT(DISTINCT visitor_id) as total\n                FROM events WHERE store_id = ? AND is_staff = 0 AND event_type = 'ENTRY'\n            ", (store_id,)).fetchone()
            converted_row = conn.execute('\n                SELECT COUNT(DISTINCT visitor_id) as converted\n                FROM sessions WHERE store_id = ? AND is_staff = 0 AND did_purchase = 1\n            ', (store_id,)).fetchone()
        total = total_row['total'] if total_row else 0
        converted = converted_row['converted'] if converted_row else 0
        if total < 5:
            return []
        current_rate = converted / total if total > 0 else 0.0
        historical_rate = get_historical_conversion_rate(store_id, days=7)
        if historical_rate is None or historical_rate == 0:
            return []
        drop_ratio = (historical_rate - current_rate) / historical_rate
        if drop_ratio >= CONVERSION_DROP_THRESHOLD:
            severity = AnomalySeverity.CRITICAL if drop_ratio >= 0.4 else AnomalySeverity.WARN
            anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='CONVERSION_DROP', severity=severity, store_id=store_id, zone_id=None, detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description=f"Today's conversion rate ({current_rate:.1%}) is {drop_ratio:.0%} below the 7-day average ({historical_rate:.1%}).", suggested_action='Escalate to store manager. Check for staffing issues, pricing anomalies, or inventory shortfalls.' if severity == AnomalySeverity.CRITICAL else 'Review staff availability and active promotions.', metric_value=round(current_rate, 4), threshold_value=round(historical_rate * (1 - CONVERSION_DROP_THRESHOLD), 4)))
    except Exception as e:
        logger.error(f'Conversion drop detection error: {e}')
    return anomalies

def _detect_dead_zones(store_id: str, now: datetime) -> List[Anomaly]:
    anomalies = []
    try:
        with get_db() as conn:
            recent_zones = conn.execute("\n                SELECT DISTINCT zone_id FROM events\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')\n                  AND zone_id IS NOT NULL\n                  AND timestamp > datetime('now', ? || ' minutes')\n            ", (store_id, f'-{DEAD_ZONE_MINUTES}')).fetchall()
            active_zones = {r['zone_id'] for r in recent_zones}
            for zone in PRODUCT_ZONES:
                if zone not in active_zones:
                    ever_visited = conn.execute("\n                        SELECT 1 FROM events\n                        WHERE store_id = ? AND zone_id = ? AND event_type = 'ZONE_ENTER'\n                        LIMIT 1\n                    ", (store_id, zone)).fetchone()
                    if ever_visited:
                        anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='DEAD_ZONE', severity=AnomalySeverity.INFO, store_id=store_id, zone_id=zone, detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description=f"Zone '{zone}' has had no customer visits in the last {DEAD_ZONE_MINUTES} minutes.", suggested_action=f'Check display arrangement and product availability in {zone} zone. Consider moving promotional signage.', metric_value=0.0, threshold_value=float(DEAD_ZONE_MINUTES)))
    except Exception as e:
        logger.error(f'Dead zone detection error: {e}')
    return anomalies

def _detect_stale_feed(store_id: str, now: datetime) -> List[Anomaly]:
    anomalies = []
    try:
        last_ts_str = get_last_event_timestamp(store_id)
        if last_ts_str is None:
            anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='STALE_FEED', severity=AnomalySeverity.WARN, store_id=store_id, zone_id=None, detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description='No events have been received from this store.', suggested_action='Check camera connectivity and pipeline health.', metric_value=None, threshold_value=float(STALE_FEED_MINUTES)))
            return anomalies
        last_ts = datetime.fromisoformat(last_ts_str.replace('Z', '+00:00'))
        lag_minutes = (now - last_ts).total_seconds() / 60
        if lag_minutes > STALE_FEED_MINUTES:
            severity = AnomalySeverity.CRITICAL if lag_minutes > 60 else AnomalySeverity.WARN
            anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='STALE_FEED', severity=severity, store_id=store_id, zone_id=None, detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description=f'Last event received {lag_minutes:.0f} minutes ago (threshold: {STALE_FEED_MINUTES} min). Feed may be stale.', suggested_action='Immediately check camera connectivity, pipeline process health, and network connectivity for this store.', metric_value=round(lag_minutes, 1), threshold_value=float(STALE_FEED_MINUTES)))
    except Exception as e:
        logger.error(f'Stale feed detection error: {e}')
    return anomalies

def _detect_crowd_buildup(store_id: str, now: datetime) -> List[Anomaly]:
    anomalies = []
    try:
        with get_db() as conn:
            rows = conn.execute("\n                SELECT zone_id, COUNT(DISTINCT visitor_id) as occupancy\n                FROM events e1\n                WHERE store_id = ?\n                  AND is_staff = 0\n                  AND event_type = 'ZONE_ENTER'\n                  AND zone_id IS NOT NULL\n                  AND NOT EXISTS (\n                      SELECT 1 FROM events e2\n                      WHERE e2.visitor_id = e1.visitor_id\n                        AND e2.store_id = e1.store_id\n                        AND e2.event_type = 'ZONE_EXIT'\n                        AND e2.zone_id = e1.zone_id\n                        AND e2.timestamp > e1.timestamp\n                  )\n                GROUP BY zone_id\n                HAVING occupancy > ?\n            ", (store_id, CROWD_BUILD_THRESHOLD)).fetchall()
            for row in rows:
                anomalies.append(Anomaly(anomaly_id=str(uuid.uuid4()), anomaly_type='CROWD_BUILD', severity=AnomalySeverity.WARN, store_id=store_id, zone_id=row['zone_id'], detected_at=now.strftime('%Y-%m-%dT%H:%M:%SZ'), description=f"Zone '{row['zone_id']}' has {row['occupancy']} customers — threshold is {CROWD_BUILD_THRESHOLD}.", suggested_action=f"Deploy additional staff to {row['zone_id']} zone. Ensure clear navigation paths.", metric_value=float(row['occupancy']), threshold_value=float(CROWD_BUILD_THRESHOLD)))
    except Exception as e:
        logger.error(f'Crowd buildup detection error: {e}')
    return anomalies

def _persist_anomalies(anomalies: List[Anomaly]) -> None:
    if not anomalies:
        return
    try:
        with get_db() as conn:
            for a in anomalies:
                conn.execute('\n                    INSERT OR REPLACE INTO anomaly_log\n                    (anomaly_id, anomaly_type, severity, store_id, zone_id,\n                     detected_at, description, suggested_action, metric_value, threshold_value)\n                    VALUES (?,?,?,?,?,?,?,?,?,?)\n                ', (a.anomaly_id, a.anomaly_type, a.severity, a.store_id, a.zone_id, a.detected_at, a.description, a.suggested_action, a.metric_value, a.threshold_value))
    except Exception as e:
        logger.error(f'Failed to persist anomalies: {e}')