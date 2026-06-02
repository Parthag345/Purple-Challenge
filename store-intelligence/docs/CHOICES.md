# Engineering Choices — Store Intelligence System

## Decision 1: Detection Model — YOLOv8n + ByteTrack

### Options Considered

| Option | Pros | Cons |
|---|---|---|
| **YOLOv8n + ByteTrack** | Fast CPU inference, good accuracy, active community | Lower accuracy on small/occluded targets vs larger models |
| YOLOv8m + DeepSORT | Better detection accuracy, appearance-based tracking | Needs GPU for real-time, DeepSORT fails in crowded scenes |
| RT-DETR + StrongSORT | State-of-the-art detection, better occlusion handling | Much heavier, slower, complex setup |
| MediaPipe Pose | Works on CPU, good for body pose | Limited to single-person tracking, not retail-grade |
| GPT-4V / Gemini Vision | Semantic understanding (staff vs customer) | Not suitable for real-time 15fps processing, expensive per frame |

### What AI Suggested

Claude suggested RT-DETR for "best accuracy in crowded scenes." Gemini suggested GPT-4V for zone classification since the brief explicitly allowed VLMs.

### What I Chose and Why

**YOLOv8n for detection + ByteTrack for tracking.**

Reasoning: The brief evaluates *how I handle uncertainty* not detection rate perfection. YOLOv8n runs at ~30fps on CPU at 640px resolution — this means real-time processing of 15fps CCTV without GPU. ByteTrack's IOU-based matching is more robust in retail crowded scenes than DeepSORT's appearance-only matching.

I did *not* use a VLM per-frame because GPT-4V would cost ~$0.01 per frame × 15fps × 3 cameras × 1 hour = ~$1,620 per store per hour. Impractical. VLMs are used selectively: I would apply them for periodic batch classification (e.g., "is this person's dominant torso colour navy blue?") not per-frame inference.

**On the partial occlusion edge case**: YOLOv8 degrades gracefully — confidence drops, but the detection is still emitted. I set `MIN_CONFIDENCE = 0.35` (not 0.5) and never suppress low-confidence events — they are flagged in the event schema so the API can apply confidence weighting.

---

## Decision 2: Event Schema Design

### Options Considered

**Option A — Flat schema, all fields at top level:**
```json
{"event_id": "...", "store_id": "...", "queue_depth": 3, "sku_zone": "MOISTURISER"}
```
- Simple but inflexible — every new field requires schema migration

**Option B — Nested metadata object (chosen):**
```json
{"event_id": "...", "store_id": "...", "metadata": {"queue_depth": 3, "sku_zone": "MOISTURISER"}}
```
- Extensible, backward-compatible, single events table

**Option C — Separate schemas per event type:**
```json
BillingQueueJoinEvent, ZoneDwellEvent, EntryEvent...
```
- Type-safe but requires multiple tables and complex JOIN queries for analytics

### What AI Suggested

GPT-4 suggested Option C (polymorphic event types) for "type safety." Claude suggested Option B.

### What I Chose and Why

**Option B — nested metadata.** The key insight is that analytics queries (conversion rate, funnel, dwell) need to scan across event types. With Option C, computing funnel stage counts requires 4 JOINs across 4 tables. With Option B, it's a single `WHERE event_type IN (...)` query. SQLite is already fast at column projection — there's no reason to normalize the metadata fields into separate tables.

I overrode the AI on one sub-decision: confidence was initially proposed as optional. I made it required (0.0–1.0) because the scoring criteria specifically calls out "confidence calibration" — if confidence is optional, detectors can simply omit it for low-confidence detections, which is the exact silent-failure mode we're trying to prevent.

**Schema compliance enforcement**: The `StoreEvent` Pydantic model validates UUID format for event_id, ISO-8601 for timestamp, and range for confidence. Invalid events return structured errors in the ingest response — they don't block the rest of the batch (partial success).

---

## Decision 3: API Architecture — SQLite with WAL + FastAPI

### Options Considered

| Option | Pros | Cons |
|---|---|---|
| **SQLite WAL + FastAPI** | Zero infrastructure, runs in docker compose, fast reads | Single-writer, not horizontally scalable |
| PostgreSQL + FastAPI | Production-grade concurrency, JSON operators | Adds operational complexity, needs separate container config |
| Redis Streams + FastAPI | Real-time event streaming built-in | Complex setup, no SQL analytics, persistence not default |
| TimescaleDB | Excellent time-series analytics | Heavy setup, overkill for challenge scale |

### What AI Suggested

Multiple AI tools recommended PostgreSQL. One argued SQLite "isn't production-ready." Another suggested Redis Streams for the event pipeline.

### What I Chose and Why

**SQLite with WAL mode + FastAPI.**

I pushed back on the "SQLite isn't production" claim. At the scale of this challenge (40 stores, 15fps video, ~500 events/batch), SQLite handles the workload comfortably. WAL (Write-Ahead Logging) mode enables concurrent reads — multiple API workers can query while the ingestion worker writes. The `timeout=30` on connection handles brief write contention.

The actual constraint is horizontal scaling — at 40 stores with real-time streaming, SQLite would bottleneck on writes. That's the correct answer to the follow-up question: "What breaks at 40 live stores?" The answer is the single SQLite writer. Migration path: swap `get_db()` to use asyncpg + PostgreSQL, schema stays identical.

**Why FastAPI over Flask or Node.js**: FastAPI's Pydantic integration validates the event schema at the request boundary, not in application code. This means malformed events are rejected with structured 422 responses before they touch the database. FastAPI's async support also allows the ingestion endpoint to handle burst traffic without blocking the metrics endpoints.

**Idempotency implementation**: The `POST /events/ingest` endpoint uses a bulk `SELECT event_id FROM events WHERE event_id IN (...)` before insertion to identify duplicates. This is a single round-trip for the entire batch. The `INSERT OR IGNORE` SQL then handles any race conditions between the check and the insert (though SQLite's serialized writes make this nearly impossible).

---

## Summary Table

| Decision | Options | AI Suggested | Chose | Reason for Override/Agreement |
|---|---|---|---|---|
| Detection model | YOLOv8n, RT-DETR, GPT-4V | RT-DETR / GPT-4V | YOLOv8n | CPU real-time, cost of VLM per-frame is impractical |
| Event schema | Flat, Nested, Per-type | Per-type schemas | Nested metadata | Single-table analytics queries; extensibility |
| Database | SQLite, PostgreSQL, Redis | PostgreSQL | SQLite WAL | Zero infra for docker compose; scales to challenge volume |
| Confidence handling | Optional (AI default) | Optional | Required | Brief specifically penalizes silent dropping of low-conf events |
| POS correlation window | 5-min pre, 2-min post | 5-min + 2-min post | 5-min pre only | Post-transaction window introduces false positives from returns |
