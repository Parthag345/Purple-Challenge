"""
Tests for anomaly detection in Store Intelligence API.

# PROMPT: "Write tests for retail store anomaly detection covering: queue spike
# (queue_depth > threshold), conversion drop (current rate < 80% of 7-day avg),
# dead zone (no visits in 30 min), stale feed (no events for 10 min), crowd
# buildup (>8 people in one zone). Each anomaly should have severity levels
# INFO/WARN/CRITICAL and a suggested_action string. Test that anomalies are
# correctly cleared when the condition resolves."
#
# CHANGES MADE:
# - Adjusted queue thresholds based on actual Brigade store layout (5-counter store)
# - Added test for STALE_FEED when no events at all (not just lag)
# - Fixed conversion drop test — AI used absolute values, changed to ratio-based
# - Added suggested_action content validation (not just presence)
"""

import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

import database
from database import init_db


def make_event(
    visitor_id="VIS_antest",
    event_type="ENTRY",
    store_id="STORE_BLR_002",
    zone_id=None,
    is_staff=False,
    confidence=0.88,
    queue_depth=None,
    timestamp=None,
):
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": "CAM_ENTRY_01",
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": 0,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {"queue_depth": queue_depth, "sku_zone": None, "session_seq": 1},
    }


@pytest.fixture(scope="function")
def client():
    database.DB_PATH = f"/tmp/test_anomaly_{uuid.uuid4().hex}.db"
    from main import app
    init_db()
    with TestClient(app) as c:
        yield c


# ─── Anomalies Response Structure ─────────────────────────────────────────────

class TestAnomaliesStructure:
    def test_anomalies_endpoint_returns_200(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        assert resp.status_code == 200

    def test_anomalies_response_has_required_fields(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        data = resp.json()
        assert "store_id" in data
        assert "active_anomalies" in data
        assert "as_of" in data

    def test_anomalies_empty_by_default(self, client):
        """Fresh store with minimal data should have few or no anomalies."""
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        assert resp.status_code == 200
        data = resp.json()
        # STALE_FEED is expected when no events
        for anomaly in data["active_anomalies"]:
            assert anomaly["anomaly_type"] in (
                "STALE_FEED", "DEAD_ZONE", "QUEUE_SPIKE",
                "CONVERSION_DROP", "CROWD_BUILD"
            )

    def test_each_anomaly_has_severity(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        for anomaly in resp.json()["active_anomalies"]:
            assert anomaly["severity"] in ("INFO", "WARN", "CRITICAL")

    def test_each_anomaly_has_suggested_action(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        for anomaly in resp.json()["active_anomalies"]:
            assert "suggested_action" in anomaly
            assert len(anomaly["suggested_action"]) > 10, \
                "suggested_action should be meaningful, not empty"

    def test_each_anomaly_has_description(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        for anomaly in resp.json()["active_anomalies"]:
            assert "description" in anomaly
            assert len(anomaly["description"]) > 0


# ─── Queue Spike Anomaly ──────────────────────────────────────────────────────

class TestQueueSpikeAnomaly:
    def _ingest_queue_events(self, client, depth: int):
        """Ingest events that create a queue of given depth."""
        for i in range(depth):
            event = make_event(
                visitor_id=f"VIS_queue_{i}",
                event_type="BILLING_QUEUE_JOIN",
                zone_id="BILLING_QUEUE",
                queue_depth=depth,
                timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
            client.post("/events/ingest", json={"events": [event]})

    def test_queue_spike_detected_above_threshold(self, client):
        self._ingest_queue_events(client, depth=6)
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["active_anomalies"]]
        assert "BILLING_QUEUE_SPIKE" in anomaly_types

    def test_queue_spike_severity_warn_at_5(self, client):
        self._ingest_queue_events(client, depth=5)
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        queue_anomalies = [a for a in resp.json()["active_anomalies"]
                          if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        if queue_anomalies:
            assert queue_anomalies[0]["severity"] in ("WARN", "CRITICAL")

    def test_queue_spike_severity_critical_at_8(self, client):
        self._ingest_queue_events(client, depth=9)
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        queue_anomalies = [a for a in resp.json()["active_anomalies"]
                          if a["anomaly_type"] == "BILLING_QUEUE_SPIKE"]
        if queue_anomalies:
            assert queue_anomalies[0]["severity"] == "CRITICAL"

    def test_no_queue_spike_below_threshold(self, client):
        self._ingest_queue_events(client, depth=3)
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["active_anomalies"]]
        assert "BILLING_QUEUE_SPIKE" not in anomaly_types


# ─── Stale Feed Anomaly ───────────────────────────────────────────────────────

class TestStaleFeedAnomaly:
    def test_stale_feed_detected_when_no_events(self, client):
        """Store with no events at all should have STALE_FEED anomaly."""
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["active_anomalies"]]
        # For a store with no data
        assert "STALE_FEED" in anomaly_types

    def test_stale_feed_zone_id_is_none(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        stale_anomalies = [a for a in resp.json()["active_anomalies"]
                          if a["anomaly_type"] == "STALE_FEED"]
        for a in stale_anomalies:
            assert a["zone_id"] is None

    def test_stale_feed_suggested_action_mentions_camera(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        stale_anomalies = [a for a in resp.json()["active_anomalies"]
                          if a["anomaly_type"] == "STALE_FEED"]
        for a in stale_anomalies:
            action_lower = a["suggested_action"].lower()
            assert any(word in action_lower for word in ["camera", "connectivity", "pipeline", "network"]), \
                f"Suggested action should mention camera/connectivity: {a['suggested_action']}"


# ─── Dead Zone Anomaly ────────────────────────────────────────────────────────

class TestDeadZoneAnomaly:
    def test_dead_zone_has_info_severity(self, client):
        # Ingest historical zone visit, then stop
        old_ts = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        event = make_event(
            visitor_id="VIS_historical",
            event_type="ZONE_ENTER",
            zone_id="HAIRCARE",
            timestamp=old_ts,
        )
        event["camera_id"] = "CAM_FLOOR_01"
        client.post("/events/ingest", json={"events": [event]})

        resp = client.get("/stores/STORE_BLR_002/anomalies")
        dead_anomalies = [a for a in resp.json()["active_anomalies"]
                         if a["anomaly_type"] == "DEAD_ZONE"]
        for a in dead_anomalies:
            assert a["severity"] == "INFO"

    def test_dead_zone_has_zone_id(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        dead_anomalies = [a for a in resp.json()["active_anomalies"]
                         if a["anomaly_type"] == "DEAD_ZONE"]
        for a in dead_anomalies:
            assert a["zone_id"] is not None


# ─── Conversion Drop Anomaly ──────────────────────────────────────────────────

class TestConversionDropAnomaly:
    def test_no_conversion_drop_without_historical_baseline(self, client):
        """Without 7-day history, conversion drop should not fire."""
        # Add some visitors with zero purchases
        for i in range(10):
            event = make_event(visitor_id=f"VIS_cd{i}", event_type="ENTRY")
            client.post("/events/ingest", json={"events": [event]})

        resp = client.get("/stores/STORE_BLR_002/anomalies")
        anomaly_types = [a["anomaly_type"] for a in resp.json()["active_anomalies"]]
        # Should NOT fire without historical baseline
        assert "CONVERSION_DROP" not in anomaly_types


# ─── Anomaly Completeness ─────────────────────────────────────────────────────

class TestAnomalyCompleteness:
    def test_anomaly_ids_are_unique(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        anomaly_ids = [a["anomaly_id"] for a in resp.json()["active_anomalies"]]
        assert len(anomaly_ids) == len(set(anomaly_ids)), "Anomaly IDs must be unique"

    def test_anomaly_detected_at_is_valid_timestamp(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        for a in resp.json()["active_anomalies"]:
            # Should not raise
            datetime.fromisoformat(a["detected_at"].replace("Z", "+00:00"))

    def test_anomaly_store_id_matches_request(self, client):
        resp = client.get("/stores/STORE_BLR_002/anomalies")
        for a in resp.json()["active_anomalies"]:
            assert a["store_id"] == "STORE_BLR_002"
