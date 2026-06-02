# Store Intelligence System — Architecture Design

## Overview

This document describes the architecture of the Store Intelligence system built for the Purplle Tech Challenge 2026. The system transforms raw CCTV footage into live business analytics, centred around the **Offline Store Conversion Rate** as its North Star metric.

```
Raw CCTV Clips
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  Detection Pipeline (pipeline/)                       │
│                                                       │
│  YOLOv8n (person detection) → ByteTrack (tracking)  │
│       │                                               │
│  DirectionClassifier → ENTRY / EXIT decision         │
│       │                                               │
│  ReIDEngine (colour histogram + bbox trajectory)      │
│       │                                               │
│  StaffDetector (colour + zone variety + dwell time)  │
│       │                                               │
│  ZoneClassifier (position → zone_id mapping)         │
│       │                                               │
│  EventEmitter → events.jsonl (JSONL stream)          │
└──────────────────────────────────────────────────────┘
     │
     ▼ POST /events/ingest (batch 500)
┌──────────────────────────────────────────────────────┐
│  Intelligence API (app/)                              │
│                                                       │
│  FastAPI + Uvicorn                                    │
│       │                                               │
│  Ingestion Layer:                                     │
│    - Pydantic schema validation                       │
│    - event_id idempotency deduplication               │
│    - Session materialization                          │
│    - POS transaction correlation (5-min window)       │
│       │                                               │
│  SQLite (WAL mode) — events, sessions, pos_transactions│
│       │                                               │
│  Metric Computation (real-time, not cached):          │
│    - /metrics  — conversion rate, dwell, queue        │
│    - /funnel   — stage-by-stage drop-off              │
│    - /heatmap  — zone frequency normalised 0-100      │
│    - /anomalies — queue spike, dead zone, stale feed  │
│    - /health   — STALE_FEED warning for on-call       │
└──────────────────────────────────────────────────────┘
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  Live Dashboard (dashboard/)                          │
│  Rich terminal UI — metrics update every 3 seconds   │
│  Shows: metrics, funnel, zone heatmap, anomalies     │
└──────────────────────────────────────────────────────┘
```

---

## Component Design Decisions

### 1. Detection Pipeline

**YOLOv8n** was chosen as the primary person detector. The `nano` variant runs on CPU in real time at 15fps with acceptable accuracy for entry/exit counting. The pipeline is designed to upgrade to `yolov8m` (medium) for production deployments where a GPU is available.

**ByteTrack** was selected over DeepSORT because it does not require appearance features for tracking — it uses IOU-based matching which is faster and more reliable in occlusion scenarios. The appearance-based Re-ID is handled separately by the `ReIDEngine` class.

**Direction Classification**: Entry vs. exit is determined by tracking centroid trajectory across the entry line (set at 20% of frame height, mimicking the camera angle above the door). A track moving from `y < threshold` to `y > threshold` is classified as ENTRY; the reverse is EXIT. Minimum 3 frames of trajectory are required before classification to avoid false positives on stationary detections.

**Staff Detection** uses a three-signal scoring system:
1. Dominant colour of torso region (dark navy/black → staff-uniform-like)
2. Zone variety score (staff cover all zones regularly)
3. Presence duration (>4 hours → likely staff)

This avoids relying on facial recognition (not available due to blurring) and works across the store.

**Re-ID (Re-entry Detection)**: Uses colour histogram distance + entry position similarity. A returning visitor within 30 minutes with a similarity score > 0.5 is classified as REENTRY rather than a new ENTRY. This directly reduces re-entry inflation — a known vendor problem in the brief.

**Cross-Camera Deduplication**: The `_cross_cam_seen` registry tracks which camera last saw each visitor_id. When the floor camera and entry camera overlap, a visitor is deduplicated by checking if the same visitor_id was seen by another camera within 3 seconds.

---

## AI-Assisted Decisions

### 1. Event Schema Design

I used an LLM to evaluate multiple event schema options:
- **Option A**: Flat schema with all fields at the top level
- **Option B**: Nested `metadata` object for extensible fields
- **Option C**: Separate schema per event type (EventEntry, EventZoneDwell, etc.)

The AI recommended **Option B** as the best balance of forward-compatibility and query simplicity. I agreed with this — the metadata object allows queue_depth and sku_zone to be added without breaking schema compatibility, and a single events table is simpler to query across all event types. I overrode one AI suggestion: the AI initially proposed making `confidence` optional (defaulting to null). I changed this to required with a 0.0–1.0 range because the brief specifically says "do not suppress low-confidence events" — requiring confidence forces the detection layer to always report it.

### 2. Database Choice: SQLite vs PostgreSQL

The AI recommended PostgreSQL for "production-grade concurrent writes." I pushed back: this is a challenge submission that runs via `docker compose up` — PostgreSQL adds operational complexity without benefit at this data volume. SQLite with WAL mode handles concurrent reads and single-writer workloads efficiently. The `get_db()` context manager uses WAL journal mode and `synchronous=NORMAL` for safe concurrent access. I documented this in CHOICES.md.

### 3. POS Correlation Window

The brief specifies "visitor in the billing zone in the 5-minute window before a transaction." The AI suggested also considering a 2-minute post-transaction window (visitor might still be in billing after POS event). I evaluated both but chose the strict 5-minute pre-transaction window because: (a) it's what the brief specifies, (b) including post-transaction events would count staff processing returns as customer conversions, and (c) a visitor who appears in the billing zone 1 minute *after* a transaction is more likely a new customer than the one who just purchased.

---

## Edge Case Handling

| Edge Case | Approach |
|---|---|
| Group entry (2-4 together) | Each detection gets unique track_id from ByteTrack → individual ENTRY events |
| Staff movement | Three-signal staff classifier; `is_staff=True` events excluded from all customer metrics |
| Re-entry | Re-ID engine matches returning visitors within 30-min window; emits REENTRY not second ENTRY |
| Partial occlusion | Low-confidence detections (≥0.35) are kept and flagged; they're never dropped silently |
| Billing queue buildup | BILLING_QUEUE_JOIN events carry `queue_depth`; anomaly fires at depth > 5 |
| Empty store periods | All metrics return 0/0.0 gracefully; zero-division is protected throughout |
| Camera overlap | `_cross_cam_seen` registry; 3-second dedup window prevents double-counting |
| Stale feed | `/health` returns `STALE_FEED` if last event > 10 minutes ago; checked at ingested_at level |

---

## Data Flow: North Star Metric

```
ENTRY events                    → unique_visitors (denominator)
        ↓
ZONE_ENTER / BILLING_QUEUE_JOIN  → session materialization
        ↓
POS transaction correlation      → did_purchase = True on session
(5-minute billing zone window)
        ↓
Sessions with did_purchase=True  → conversions (numerator)
        ↓
conversion_rate = conversions / unique_visitors
```

Every pipeline decision is evaluated against this chain. Staff exclusion protects the denominator. Re-ID deduplication prevents denominator inflation. POS window correlation makes the numerator meaningful.
