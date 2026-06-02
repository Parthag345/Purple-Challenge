import logging
import time
from datetime import datetime, timezone
from typing import List
from database import check_db_health, get_db
from metrics import get_active_visitor_count, get_events_last_hour, get_last_event_timestamp
from models import HealthResponse, StoreHealthStatus
logger = logging.getLogger(__name__)
START_TIME = time.time()
API_VERSION = '1.0.0'
STALE_FEED_MINUTES = 10

def get_health() -> HealthResponse:
    now = datetime.now(timezone.utc)
    uptime = time.time() - START_TIME
    db_ok = check_db_health()
    db_status = 'healthy' if db_ok else 'unavailable'
    store_statuses: List[StoreHealthStatus] = []
    try:
        with get_db() as conn:
            store_rows = conn.execute('\n                SELECT DISTINCT store_id FROM events\n                UNION\n                SELECT DISTINCT store_id FROM sessions\n            ').fetchall()
            for row in store_rows:
                sid = row['store_id']
                last_ts = get_last_event_timestamp(sid)
                events_1h = get_events_last_hour(sid)
                active_visitors = get_active_visitor_count(sid)
                feed_status = 'NO_DATA'
                if last_ts:
                    last_dt = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                    lag_m = (now - last_dt).total_seconds() / 60
                    if lag_m <= STALE_FEED_MINUTES:
                        feed_status = 'LIVE'
                    else:
                        feed_status = 'STALE_FEED'
                store_statuses.append(StoreHealthStatus(store_id=sid, last_event_timestamp=last_ts, feed_status=feed_status, events_last_hour=events_1h, active_visitors=active_visitors))
    except Exception as e:
        logger.error(f'Health check store enumeration failed: {e}')
        db_status = 'degraded'
    if not db_ok:
        overall_status = 'unhealthy'
    elif any((s.feed_status == 'STALE_FEED' for s in store_statuses)):
        overall_status = 'degraded'
    else:
        overall_status = 'healthy'
    return HealthResponse(status=overall_status, version=API_VERSION, uptime_seconds=round(uptime, 1), stores=store_statuses, database_status=db_status, timestamp=now.strftime('%Y-%m-%dT%H:%M:%SZ'))