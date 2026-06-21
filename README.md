# Store Intelligence — Offline Retail Analytics Platform

A complete computer vision and analytics platform built for the **Purplle Tech Challenge 2026**. This system transforms raw CCTV footage into live store business metrics—centered around the **Offline Store Conversion Rate** as the primary KPI.

---

## 🏗️ Architecture Overview

The platform operates as a three-tier system: the Edge Pipeline, the Intelligence API, and the Real-time Dashboard.

```
                  [ Raw CCTV Clips (CAM 1-5.mp4) ]
                                 │
                                 ▼
 ┌──────────────────────────────────────────────────────────────┐
 │  1. Edge Detection & Tracking Pipeline (pipeline/)           │
 │                                                              │
 │  YOLOv8n (Person Detection) ──► ByteTrack (Object Tracking)  │
 │                                        │                     │
 │  Staff Filtering ◄── Re-ID Engine ◄────┘                     │
 │  (Uniform/Dwell)      (Histograms)                           │
 │                                                              │
 │  Direction Classifier (ENTRY/EXIT)                           │
 │  Zone Classifier (Layout Mapping)                            │
 │                                                              │
 │  Event Emitter ──► events.jsonl Stream                       │
 └───────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼ POST /events/ingest (Batch of 500)
 ┌──────────────────────────────────────────────────────────────┐
 │  2. Intelligence API (app/)                                  │
 │                                                              │
 │  FastAPI Server (Asynchronous & Validated via Pydantic)      │
 │                                                              │
 │  Ingestion Layer:                                            │
 │    ├── Idempotency Check (Duplicate event_id filtering)      │
 │    ├── Session Materializer (Dwell times & entry/exit state) │
 │    └── POS Transaction Correlator (5-min billing-zone window)│
 │                                                              │
 │  SQLite Database (WAL Mode for high concurrency)             │
 └───────────────────────────────┬──────────────────────────────┘
                                 │
                                 ▼ REST API Polls / WebSocket
 ┌──────────────────────────────────────────────────────────────┐
 │  3. Live Terminal Dashboard (dashboard/)                     │
 │                                                              │
 │  Rich Terminal UI (Autorefresh every 3 seconds)              │
 │  Displays: Store KPIs, Funnel Drop-off, Heatmap, Anomalies  │
 └──────────────────────────────────────────────────────────────┘
```

---

## 🛠️ Tech Stack & Key Choices

| Component | Selected Technology | Alternative Evaluated | Trade-off Justification |
| :--- | :--- | :--- | :--- |
| **Detection** | **YOLOv8n** | RT-DETR / GPT-4V | YOLOv8n delivers 30+ fps on CPU for real-time video feeds. VLM inference (GPT-4V) per-frame is cost-prohibitive. |
| **Tracking** | **ByteTrack** | DeepSORT | ByteTrack uses IOU-based matching, which is computationally cheaper and highly robust against occlusion in crowded aisles. |
| **Database** | **SQLite (WAL)** | PostgreSQL / Redis | Zero infrastructure overhead (runs out-of-the-box in Docker). Write-Ahead Logging (WAL) handles concurrent read queries and store traffic scaling cleanly. |
| **API** | **FastAPI** | Flask / Express | Built-in async event-loop and automatic schema validation at the gateway via Pydantic. |

---

## 📂 Project Structure

```
store-intelligence/
├── pipeline/
│   ├── detect.py          # Detection logic, YOLO tracker & simulation mode
│   ├── tracker.py         # Person tracker, Re-ID engine & staff filtering
│   ├── emit.py            # Event schema definitions & API post utility
│   └── run.sh             # Pipeline shell script launcher
├── app/
│   ├── main.py            # FastAPI setup, middleware & routes
│   ├── models.py          # Pydantic schemas for database & requests
│   ├── database.py        # SQLite schema initialization
│   ├── ingestion.py       # Deduplication, session storage & POS correlation
│   ├── metrics.py         # Store KPI aggregations
│   ├── funnel.py          # Funnel and heatmap computations
│   ├── anomalies.py       # Alarm system (e.g., long queues, dead zones)
│   └── health.py          # Health status checks & stale feed notifications
├── tests/
│   ├── test_pipeline.py   # Tracker & detector unit tests
│   ├── test_metrics.py    # FastAPI metrics & conversion logic tests
│   └── test_anomalies.py  # Anomaly rule assertion tests
├── dashboard/
│   ├── live_dashboard.py  # Rich-based interactive terminal UI
│   └── web_dashboard.html # HTML/JS alternative web dashboard
├── docs/
│   ├── DESIGN.md          # Full architectural details
│   └── CHOICES.md         # In-depth architectural trade-offs & justifications
└── requirements.txt       # Project python dependencies
```

---

## 🚀 Quick Start (5 Commands)

To run the complete system (FastAPI server + CCTV processing pipeline + TUI dashboard) follow these steps:

### 1. Build and Start the Docker containers
From the root directory:
```bash
docker compose up --build
```
This spawns:
- **FastAPI API Server** on `http://localhost:8000`
- **Detection Pipeline Worker** ready to accept videos

### 2. Verify API Health
Ensure the API is online:
```bash
curl http://localhost:8000/health
```

### 3. Load the Datasets & CCTV Clips
To process the challenge video files (located under the `uploads/` directory):
```bash
# Processes CAM 1 to CAM 5 video clips and correlates with POS CSV data
docker compose exec pipeline python /app/scripts/process_brigade_cctv.py --clips-dir "/app/uploads/" --api-url "http://app:8000"
```

*Alternatively, if you want a simulated event stream calibrated directly to the Brigade Road dataset patterns (no videos required):*
```bash
# Simulates CCTV visitor tracks and posts them directly to the API
docker compose exec pipeline python /app/pipeline/detect.py --simulate --post-to-api --api-url "http://app:8000"
```

### 4. Query Store Metrics
Check the computed real-time KPIs for the Brigade Road store (`STORE_BLR_002`):
```bash
curl http://localhost:8000/stores/STORE_BLR_002/metrics
```

### 5. Launch the Live Terminal Dashboard
Launch the Rich terminal dashboard to monitor the live metric stream:
```bash
python store-intelligence/dashboard/live_dashboard.py --store STORE_BLR_002 --api http://localhost:8000
```
---

## 🧪 Running Unit Tests

The system comes with a comprehensive test suite (74 passing tests) covering detection, tracking, metrics, and anomalies:

```bash
# Install development packages
pip install -r store-intelligence/requirements.txt

# Run pytest with code coverage output
pytest store-intelligence/tests/ -v --cov=store-intelligence/app --cov=store-intelligence/pipeline
```

---

## 💡 Key Features & Retail Logic

- **Staff Exclusion:** Employs a three-signal classifier (dominant uniform color + zone variety + occupancy duration) to filter out employees and avoid inflating visitor counts.
- **Visitor Re-ID:** Color histogram matching prevents double-counting customers who leave and re-enter within a 30-minute window.
- **Idempotent Ingestion:** The ingestion layer filters out duplicate events based on a unique UUID4.
- **POS Correlation:** Uses a 5-minute pre-transaction billing zone window to accurately associate CCTV sessions with sales receipts.
- **Stale Feed Watchdog:** Automatically triggers warnings in the `/health` endpoint if camera feeds stop updating for over 10 minutes.
