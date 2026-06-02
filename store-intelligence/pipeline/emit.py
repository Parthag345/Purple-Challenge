import uuid
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field, asdict
from enum import Enum
logger = logging.getLogger(__name__)

class EventType(str, Enum):
    ENTRY = 'ENTRY'
    EXIT = 'EXIT'
    ZONE_ENTER = 'ZONE_ENTER'
    ZONE_EXIT = 'ZONE_EXIT'
    ZONE_DWELL = 'ZONE_DWELL'
    BILLING_QUEUE_JOIN = 'BILLING_QUEUE_JOIN'
    BILLING_QUEUE_ABANDON = 'BILLING_QUEUE_ABANDON'
    REENTRY = 'REENTRY'

@dataclass
class EventMetadata:
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = 0
    group_size: Optional[int] = None
    occlusion_flag: bool = False
    reentry_count: int = 0

@dataclass
class StoreEvent:
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str]
    dwell_ms: int
    is_staff: bool
    confidence: float
    metadata: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    def validate(self) -> List[str]:
        errors = []
        if not self.event_id:
            errors.append('event_id is required')
        if not self.store_id:
            errors.append('store_id is required')
        if not self.camera_id:
            errors.append('camera_id is required')
        if not self.visitor_id:
            errors.append('visitor_id is required')
        if self.event_type not in [e.value for e in EventType]:
            errors.append(f'Invalid event_type: {self.event_type}')
        if self.dwell_ms < 0:
            errors.append('dwell_ms must be >= 0')
        if not 0.0 <= self.confidence <= 1.0:
            errors.append(f'confidence must be between 0 and 1, got {self.confidence}')
        try:
            datetime.fromisoformat(self.timestamp.replace('Z', '+00:00'))
        except ValueError:
            errors.append(f'Invalid timestamp format: {self.timestamp}')
        return errors

def make_event(store_id: str, camera_id: str, visitor_id: str, event_type: EventType, timestamp: datetime, zone_id: Optional[str]=None, dwell_ms: int=0, is_staff: bool=False, confidence: float=0.9, metadata: Optional[Dict[str, Any]]=None) -> StoreEvent:
    if metadata is None:
        metadata = {'queue_depth': None, 'sku_zone': None, 'session_seq': 0}
    metadata.setdefault('queue_depth', None)
    metadata.setdefault('sku_zone', None)
    metadata.setdefault('session_seq', 0)
    ts_str = timestamp.strftime('%Y-%m-%dT%H:%M:%SZ')
    event = StoreEvent(event_id=str(uuid.uuid4()), store_id=store_id, camera_id=camera_id, visitor_id=visitor_id, event_type=event_type.value, timestamp=ts_str, zone_id=zone_id, dwell_ms=dwell_ms, is_staff=is_staff, confidence=confidence, metadata=metadata)
    errors = event.validate()
    if errors:
        logger.warning(f'Event validation warnings for {event.event_id}: {errors}')
    return event

class EventEmitter:

    def __init__(self, output_path: Optional[str]=None):
        self.output_path = output_path
        self._file_handle = None
        self.events_emitted = 0
        if output_path:
            self._file_handle = open(output_path, 'a')
            logger.info(f'EventEmitter writing to {output_path}')

    def emit(self, event: StoreEvent) -> None:
        line = event.to_json() + '\n'
        if self._file_handle:
            self._file_handle.write(line)
            self._file_handle.flush()
        else:
            print(line, end='')
        self.events_emitted += 1

    def emit_batch(self, events: List[StoreEvent]) -> None:
        for event in events:
            self.emit(event)

    def close(self):
        if self._file_handle:
            self._file_handle.close()
            logger.info(f'EventEmitter closed. Total events: {self.events_emitted}')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()