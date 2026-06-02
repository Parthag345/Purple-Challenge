# Store Intelligence — Purplle Tech Challenge 2026

**Offline Store Conversion Rate Analytics API** — From raw CCTV to live business metrics.

## Quick Start (5 Commands)

```bash
# 1. Clone the repository
git clone <your-repo-url> store-intelligence && cd store-intelligence

# 2. Start the API + detection pipeline
docker compose up

# 3. Verify API is running
curl http://localhost:8000/health

# 4. Ingest simulated events (or run real pipeline)
docker compose exec pipeline bash /app/pipeline/run.sh --simulate

# 5. Query live metrics
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

That's it. The API is live at **http://localhost:8000**.

---

## Architecture

```
CCTV Clips → Detection Pipeline → Event Stream → Intelligence API → Live Dashboard
```

| Stage | Technology | Purpose |
|---|---|---|
| Detection | YOLOv8n + ByteTrack | Person detection + tracking |
| Re-ID | Colour histogram + bbox trajectory | Re-entry detection |
| Event Schema | JSONL (see schema below) | Structured behavioural events |
| API | FastAPI + SQLite (WAL) | Real-time metric computation |
| Dashboard | Rich terminal UI | Live metric display |

---

## Running the Detection Pipeline

### Option A: Simulated Events (no CCTV clips needed)

```bash
# Generate synthetic events calibrated to Brigade_Bangalore data patterns
python pipeline/detect.py --simulate --store STORE_BLR_002 --output data/events.jsonl

# Then post events to the API
python pipeline/detect.py --simulate --post-to-api --api-url http://localhost:8000
```

### Option B: Real Brigade Road CCTV Clips

The 5 CCTV clips from Brigade Road, Bangalore (CAM 1–5.mp4) map to these camera types:

| Clip | Camera ID | Type | Zone Coverage |
|------|-----------|------|---------------|
| `CAM 1.mp4` | `CAM_ENTRY_01` | Entry/Exit | Entry, Exit |
| `CAM 2.mp4` | `CAM_FLOOR_01` | Main Floor | Makeup, Skincare |
| `CAM 3.mp4` | `CAM_FLOOR_01` | Main Floor | Haircare, Fragrance |
| `CAM 4.mp4` | `CAM_BILLING_01` | Billing | Billing Counter |
| `CAM 5.mp4` | `CAM_BILLING_01` | Billing | Billing Queue |

```bash
# Process Brigade Road clips (auto-detects CAM 1-5.mp4 naming)
python scripts/process_brigade_cctv.py \
  --clips-dir "../uploads/" \
  --api-url http://localhost:8000

# Or using the main pipeline runner:
bash pipeline/run.sh --video "../uploads/" --api-url http://localhost:8000
```

The pipeline falls back to calibrated simulation if `ultralytics` is not installed.

### Option C: Initialize with Brigade Dataset (Fastest)

```bash
# Initialize DB with Brigade POS data + simulated CCTV events in one command
python scripts/init_db_with_brigade_data.py
```

This loads all 24 Brigade POS transactions (INR 34,831 total) and generates
662 simulation events calibrated to actual Brigade visit patterns.

### Brigade Road Store Results

After processing Brigade data, the API returns real business metrics:

```json
{
  "store_id": "STORE_BLR_002",
  "unique_visitors": 55,
  "conversion_rate": 0.4182,
  "avg_dwell_seconds": 672.9,
  "total_transactions": 24,
  "total_revenue_inr": 34831.74,
  "data_confidence": "HIGH"
}
```

Funnel drop-off (Brigade Road, 10 April 2026):
- Entry: 55 → Zone Visit: 35 (36.4% drop) → Billing Queue: 22 (37.1% drop) → Purchase: 22


## API Endpoints

| Endpoint | Method | Description |
|---|---|---|
| `/events/ingest` | POST | Ingest up to 500 events (idempotent) |
| `/stores/{id}/metrics` | GET | Real-time KPIs |
| `/stores/{id}/funnel` | GET | Conversion funnel with drop-off % |
| `/stores/{id}/heatmap` | GET | Zone visit frequency, normalised 0-100 |
| `/stores/{id}/anomalies` | GET | Active anomalies (INFO/WARN/CRITICAL) |
| `/health` | GET | Service health + STALE_FEED warnings |
| `/docs` | GET | Interactive Swagger UI |

### Example: GET /stores/STORE_BLR_002/metrics

```json
{
  "store_id": "STORE_BLR_002",
  "as_of": "2026-04-10T16:55:00Z",
  "unique_visitors": 35,
  "conversion_rate": 0.6571,
  "avg_dwell_seconds": 847.3,
  "zone_metrics": [
    {"zone_id": "MAKEUP", "avg_dwell_seconds": 312.5, "visit_count": 54, "current_occupancy": 3}
  ],
  "current_queue_depth": 2,
  "abandonment_rate": 0.087,
  "total_transactions": 23,
  "total_revenue_inr": 28450.50,
  "data_confidence": "HIGH"
}
```

### Example: POST /events/ingest

```json
{
  "events": [{
    "event_id": "550e8400-e29b-41d4-a716-446655440000",
    "store_id": "STORE_BLR_002",
    "camera_id": "CAM_ENTRY_01",
    "visitor_id": "VIS_c8a2f1",
    "event_type": "ENTRY",
    "timestamp": "2026-04-10T14:22:10Z",
    "zone_id": null,
    "dwell_ms": 0,
    "is_staff": false,
    "confidence": 0.91,
    "metadata": {"queue_depth": null, "sku_zone": null, "session_seq": 1}
  }]
}
```

---

## Running Tests

```bash
# Install test dependencies
pip install -r requirements.txt

# Run all tests with coverage
pytest tests/ -v --cov=app --cov=pipeline --cov-report=term-missing

# Run specific test module
pytest tests/test_pipeline.py -v
pytest tests/test_metrics.py -v
pytest tests/test_anomalies.py -v
```

---

## Live Dashboard (Part E — Bonus)

```bash
# Terminal dashboard with Rich UI (recommended)
python dashboard/live_dashboard.py --store STORE_BLR_002 --api http://localhost:8000

# Simple terminal fallback (no dependencies)
python dashboard/live_dashboard.py --simple

# With Docker
docker compose --profile dashboard up dashboard
```

The dashboard shows:
- Real-time: visitors, conversion rate, queue depth, revenue
- Conversion funnel with visual bars
- Zone heatmap (normalised 0-100)
- Active anomalies with severity colours

---

## Dataset Integration

### Brigade Bangalore POS Data

The provided `Brigade_Bangalore_10_April_26` CSV is loaded automatically on API startup.
Store code `ST1008` is mapped to `STORE_BLR_002`.

```bash
# Manual load (also done automatically on startup from data/brigade_pos.csv)
python scripts/init_db_with_brigade_data.py
```

### Brigade Road CCTV Clips

```bash
# Process all 5 clips (CAM 1-5.mp4) with camera type auto-detection
python scripts/process_brigade_cctv.py \
  --clips-dir "../uploads/" \
  --api-url http://localhost:8000
```


## Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # Detection + simulation mode
│   ├── tracker.py         # ByteTrack + Re-ID + staff detection
│   ├── emit.py            # Event schema + emission
│   └── run.sh             # One-command pipeline runner
├── app/
│   ├── main.py            # FastAPI entrypoint + structured logging
│   ├── models.py          # Pydantic event schema + response models
│   ├── database.py        # SQLite with WAL, schema init
│   ├── ingestion.py       # Ingest, dedup, POS correlation
│   ├── metrics.py         # Real-time metric computation
│   ├── funnel.py          # Funnel + heatmap logic
│   ├── anomalies.py       # Anomaly detection (5 types)
│   └── health.py          # Health endpoint
├── tests/
│   ├── test_pipeline.py   # Detection + tracking tests
│   ├── test_metrics.py    # API metrics + funnel + heatmap tests
│   └── test_anomalies.py  # Anomaly detection tests
├── dashboard/
│   └── live_dashboard.py  # Rich terminal live dashboard
├── docs/
│   ├── DESIGN.md          # Architecture + AI-assisted decisions
│   └── CHOICES.md         # 3 key decisions with full reasoning
├── data/
│   ├── store_layout.json  # Zone definitions
│   └── pos_transactions.csv
├── docker-compose.yml
├── Dockerfile
├── Dockerfile.pipeline
├── requirements.txt
└── README.md
```

---

## Production Considerations

- **Scaling to 40 stores**: Replace SQLite with PostgreSQL (swap `get_db()` implementation). Schema stays identical.
- **GPU support**: Change `YOLO_MODEL = "yolov8m.pt"` and add `device="cuda"` in YOLODetector.
- **Streaming**: Replace batch ingest with Kafka consumer reading from detection layer output topic.
- **Auth**: Add API key middleware (`X-API-Key` header) before production deployment.
