from __future__ import annotations
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union
from enum import Enum
from pydantic import BaseModel, Field, field_validator, model_validator

class EventType(str, Enum):
    ENTRY = 'ENTRY'
    EXIT = 'EXIT'
    ZONE_ENTER = 'ZONE_ENTER'
    ZONE_EXIT = 'ZONE_EXIT'
    ZONE_DWELL = 'ZONE_DWELL'
    BILLING_QUEUE_JOIN = 'BILLING_QUEUE_JOIN'
    BILLING_QUEUE_ABANDON = 'BILLING_QUEUE_ABANDON'
    REENTRY = 'REENTRY'

class AnomalySeverity(str, Enum):
    INFO = 'INFO'
    WARN = 'WARN'
    CRITICAL = 'CRITICAL'

class EventMetadata(BaseModel):
    queue_depth: Optional[int] = Field(None, description='Queue depth at time of BILLING_QUEUE_JOIN')
    sku_zone: Optional[str] = Field(None, description='Zone label from store_layout.json')
    session_seq: int = Field(0, description='Ordinal position in visitor session')
    group_size: Optional[int] = Field(None, description='Number of people in group entry')
    occlusion_flag: bool = Field(False, description='Whether detection was partially occluded')
    reentry_count: int = Field(0, description='Number of times this visitor has re-entered')

class StoreEvent(BaseModel):
    event_id: str = Field(..., description='UUID v4 — globally unique')
    store_id: str = Field(..., description='Store identifier')
    camera_id: str = Field(..., description='Camera that produced this event')
    visitor_id: str = Field(..., description='Re-ID token — unique per visit session')
    event_type: EventType
    timestamp: str = Field(..., description='ISO-8601 UTC')
    zone_id: Optional[str] = Field(None, description='Zone name; null for ENTRY/EXIT')
    dwell_ms: int = Field(0, ge=0, description='Duration in ms; 0 for instantaneous events')
    is_staff: bool = Field(False, description='True if classified as staff member')
    confidence: float = Field(..., ge=0.0, le=1.0, description='Detection confidence score')
    metadata: EventMetadata = Field(default_factory=EventMetadata)

    @field_validator('timestamp')
    @classmethod
    def validate_timestamp(cls, v):
        try:
            datetime.fromisoformat(v.replace('Z', '+00:00'))
        except ValueError:
            raise ValueError(f'Invalid timestamp format: {v}. Expected ISO-8601.')
        return v

    @field_validator('event_id')
    @classmethod
    def validate_event_id(cls, v):
        try:
            uuid.UUID(v)
        except ValueError:
            raise ValueError(f'event_id must be a valid UUID: {v}')
        return v
    model_config = {'use_enum_values': True}

class IngestRequest(BaseModel):
    events: List[StoreEvent] = Field(..., max_length=500, description='Batch of events (max 500)')

class IngestEventResult(BaseModel):
    event_id: str
    status: str
    error: Optional[str] = None

class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    invalid: int
    results: List[IngestEventResult]
    trace_id: str

class ZoneDwellMetric(BaseModel):
    zone_id: str
    avg_dwell_seconds: float
    visit_count: int
    current_occupancy: int = 0

class StoreMetrics(BaseModel):
    store_id: str
    as_of: str
    unique_visitors: int
    conversion_rate: float = Field(..., description='Visitors who purchased ÷ total visitors')
    avg_dwell_seconds: float
    zone_metrics: List[ZoneDwellMetric]
    current_queue_depth: int
    abandonment_rate: float = Field(..., description='Billing queue abandons ÷ total queue joins')
    total_transactions: int
    total_revenue_inr: float
    data_confidence: str = Field('HIGH', description='HIGH/MEDIUM/LOW based on session count')

class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float = Field(0.0, description='Drop-off from previous stage (%)')

class FunnelResponse(BaseModel):
    store_id: str
    window_start: str
    window_end: str
    stages: List[FunnelStage]
    total_entries: int
    conversions: int
    overall_conversion_rate: float

class ZoneHeatmapCell(BaseModel):
    zone_id: str
    visit_frequency: int
    avg_dwell_seconds: float
    normalised_score: float = Field(..., ge=0, le=100)
    data_confidence: bool = Field(True, description='False if fewer than 20 sessions')

class HeatmapResponse(BaseModel):
    store_id: str
    as_of: str
    cells: List[ZoneHeatmapCell]

class Anomaly(BaseModel):
    anomaly_id: str
    anomaly_type: str
    severity: AnomalySeverity
    store_id: str
    zone_id: Optional[str] = None
    detected_at: str
    description: str
    suggested_action: str
    metric_value: Optional[float] = None
    threshold_value: Optional[float] = None

class AnomaliesResponse(BaseModel):
    store_id: str
    active_anomalies: List[Anomaly]
    as_of: str

class StoreHealthStatus(BaseModel):
    store_id: str
    last_event_timestamp: Optional[str]
    feed_status: str = Field(..., description='LIVE | STALE_FEED | NO_DATA')
    events_last_hour: int
    active_visitors: int

class HealthResponse(BaseModel):
    status: str
    version: str
    uptime_seconds: float
    stores: List[StoreHealthStatus]
    database_status: str
    timestamp: str

class ErrorDetail(BaseModel):
    code: str
    message: str
    field: Optional[str] = None

class ErrorResponse(BaseModel):
    error: str
    detail: List[ErrorDetail]
    trace_id: str
    timestamp: str