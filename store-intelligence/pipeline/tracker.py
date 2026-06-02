import uuid
import time
import logging
import math
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections import defaultdict
logger = logging.getLogger(__name__)

@dataclass
class BBox:
    x1: float
    y1: float
    x2: float
    y2: float

    @property
    def cx(self) -> float:
        return (self.x1 + self.x2) / 2

    @property
    def cy(self) -> float:
        return (self.y1 + self.y2) / 2

    @property
    def area(self) -> float:
        return (self.x2 - self.x1) * (self.y2 - self.y1)

    @property
    def aspect_ratio(self) -> float:
        h = self.y2 - self.y1
        w = self.x2 - self.x1
        return h / (w + 1e-06)

    def iou(self, other: 'BBox') -> float:
        ix1 = max(self.x1, other.x1)
        iy1 = max(self.y1, other.y1)
        ix2 = min(self.x2, other.x2)
        iy2 = min(self.y2, other.y2)
        inter = max(0, ix2 - ix1) * max(0, iy2 - iy1)
        union = self.area + other.area - inter
        return inter / (union + 1e-06)

    def distance_to(self, other: 'BBox') -> float:
        return math.sqrt((self.cx - other.cx) ** 2 + (self.cy - other.cy) ** 2)

@dataclass
class Detection:
    track_id: int
    bbox: BBox
    confidence: float
    frame_idx: int
    timestamp: datetime
    camera_id: str
    class_id: int = 0

@dataclass
class VisitorSession:
    visitor_id: str
    track_id: int
    camera_id: str
    store_id: str
    entry_time: Optional[datetime] = None
    exit_time: Optional[datetime] = None
    is_staff: bool = False
    is_active: bool = True
    reentry_count: int = 0
    zone_history: List[Dict] = field(default_factory=list)
    bbox_trajectory: List[BBox] = field(default_factory=list)
    appearance_features: Optional[List[float]] = None
    session_seq: int = 0
    last_seen: Optional[datetime] = None
    in_billing_zone: bool = False
    billing_zone_entry: Optional[datetime] = None

    def avg_position(self) -> Optional[Tuple[float, float]]:
        if not self.bbox_trajectory:
            return None
        cx = sum((b.cx for b in self.bbox_trajectory)) / len(self.bbox_trajectory)
        cy = sum((b.cy for b in self.bbox_trajectory)) / len(self.bbox_trajectory)
        return (cx, cy)

    def increment_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

class StaffDetector:
    STAFF_UNIFORM_COLORS = [(0, 0, 128), (0, 0, 0), (50, 50, 50), (25, 25, 112)]
    STAFF_COLOR_THRESHOLD = 0.45
    PERSISTENT_PRESENCE_HOURS = 4

    def __init__(self):
        self._staff_track_ids: set = set()

    def classify(self, session: VisitorSession, dominant_color: Optional[Tuple[int, int, int]]=None, zone_variety: int=0, presence_duration_hours: float=0.0) -> bool:
        staff_score = 0.0
        if dominant_color and self._is_staff_color(dominant_color):
            staff_score += 0.5
        if zone_variety >= 4:
            staff_score += 0.3
        elif zone_variety >= 3:
            staff_score += 0.15
        if presence_duration_hours > self.PERSISTENT_PRESENCE_HOURS:
            staff_score += 0.4
        is_staff = staff_score >= 0.5
        if is_staff:
            self._staff_track_ids.add(session.track_id)
            logger.debug(f'Track {session.track_id} classified as STAFF (score={staff_score:.2f})')
        return is_staff

    def _is_staff_color(self, color: Tuple[int, int, int]) -> bool:
        r, g, b = color
        for sr, sg, sb in self.STAFF_UNIFORM_COLORS:
            dist = math.sqrt((r - sr) ** 2 + (g - sg) ** 2 + (b - sb) ** 2)
            if dist < 80:
                return True
        brightness = (r + g + b) / 3
        return brightness < 60

class ReIDEngine:
    REENTRY_TIME_WINDOW_SECONDS = 1800
    APPEARANCE_THRESHOLD = 0.35
    POSITION_THRESHOLD = 120

    def __init__(self):
        self._exited_sessions: List[VisitorSession] = []

    def register_exit(self, session: VisitorSession) -> None:
        self._exited_sessions.append(session)

    def match(self, bbox: BBox, entry_time: datetime, appearance: Optional[List[float]]=None, camera_id: str='') -> Optional[VisitorSession]:
        candidates = []
        for session in self._exited_sessions:
            if not session.exit_time:
                continue
            elapsed = (entry_time - session.exit_time).total_seconds()
            if elapsed < 0 or elapsed > self.REENTRY_TIME_WINDOW_SECONDS:
                continue
            score = self._compute_match_score(session, bbox, appearance)
            if score > 0.5:
                candidates.append((score, session))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_score, best_session = candidates[0]
        logger.info(f'Re-ID match: new detection → {best_session.visitor_id} (score={best_score:.2f})')
        return best_session

    def _compute_match_score(self, session: VisitorSession, bbox: BBox, appearance: Optional[List[float]]) -> float:
        score = 0.0
        if appearance and session.appearance_features:
            cos_sim = self._cosine_sim(appearance, session.appearance_features)
            score += cos_sim * 0.6
        if session.bbox_trajectory:
            last_bbox = session.bbox_trajectory[-1]
            pos_dist = last_bbox.distance_to(bbox)
            pos_score = max(0, 1 - pos_dist / self.POSITION_THRESHOLD)
            score += pos_score * 0.4
        return score

    @staticmethod
    def _cosine_sim(a: List[float], b: List[float]) -> float:
        dot = sum((x * y for x, y in zip(a, b)))
        mag_a = math.sqrt(sum((x ** 2 for x in a)))
        mag_b = math.sqrt(sum((x ** 2 for x in b)))
        if mag_a == 0 or mag_b == 0:
            return 0.0
        return dot / (mag_a * mag_b)

class DirectionClassifier:
    ENTRY_LINE_Y_FRACTION = 0.2
    MIN_FRAMES_FOR_DIRECTION = 3

    def classify(self, trajectory: List[BBox], frame_height: int=1080) -> Optional[str]:
        if len(trajectory) < self.MIN_FRAMES_FOR_DIRECTION:
            return None
        entry_line = frame_height * self.ENTRY_LINE_Y_FRACTION
        first_y = trajectory[0].cy
        last_y = trajectory[-1].cy
        crossed_inward = first_y < entry_line and last_y > entry_line
        crossed_outward = first_y > entry_line and last_y < entry_line
        if crossed_inward:
            return 'ENTRY'
        elif crossed_outward:
            return 'EXIT'
        y_velocities = [trajectory[i + 1].cy - trajectory[i].cy for i in range(len(trajectory) - 1)]
        avg_vy = sum(y_velocities) / len(y_velocities)
        if avg_vy > 3:
            return 'ENTRY'
        elif avg_vy < -3:
            return 'EXIT'
        return None

class ZoneClassifier:

    def __init__(self, store_layout: Dict, camera_id: str, frame_width=1920, frame_height=1080):
        self.camera_id = camera_id
        self.frame_width = frame_width
        self.frame_height = frame_height
        self.zones = self._build_zone_map(store_layout, camera_id)

    def _build_zone_map(self, layout: Dict, camera_id: str) -> List[Dict]:
        if 'FLOOR' in camera_id:
            return [{'zone_id': 'SKINCARE', 'x1': 0, 'y1': 0, 'x2': 384, 'y2': 1080, 'sku_zone': 'MOISTURISER'}, {'zone_id': 'MAKEUP', 'x1': 384, 'y1': 0, 'x2': 768, 'y2': 1080, 'sku_zone': 'COSMETICS'}, {'zone_id': 'HAIRCARE', 'x1': 768, 'y1': 0, 'x2': 1152, 'y2': 1080, 'sku_zone': 'HAIR'}, {'zone_id': 'FRAGRANCE', 'x1': 1152, 'y1': 0, 'x2': 1536, 'y2': 1080, 'sku_zone': 'FRAGRANCE'}, {'zone_id': 'PERSONAL_CARE', 'x1': 1536, 'y1': 0, 'x2': 1920, 'y2': 1080, 'sku_zone': 'PERSONAL_CARE'}]
        elif 'BILLING' in camera_id:
            return [{'zone_id': 'BILLING_QUEUE', 'x1': 0, 'y1': 0, 'x2': 1920, 'y2': 540, 'sku_zone': None}, {'zone_id': 'BILLING', 'x1': 0, 'y1': 540, 'x2': 1920, 'y2': 1080, 'sku_zone': None}]
        return []

    def get_zone(self, bbox: BBox) -> Optional[Dict]:
        cx, cy = (bbox.cx, bbox.cy)
        for zone in self.zones:
            if zone['x1'] <= cx <= zone['x2'] and zone['y1'] <= cy <= zone['y2']:
                return zone
        return None

class VisitorTracker:
    DWELL_EMIT_INTERVAL_S = 30
    TRACK_LOST_TIMEOUT_S = 5
    CROSS_CAM_DEDUP_WINDOW_S = 3

    def __init__(self, store_id: str, store_layout: Dict):
        self.store_id = store_id
        self.store_layout = store_layout
        self.staff_detector = StaffDetector()
        self.reid_engine = ReIDEngine()
        self.dir_classifier = DirectionClassifier()
        self._active: Dict[int, VisitorSession] = {}
        self._completed: List[VisitorSession] = []
        self._cross_cam_seen: Dict[str, Dict] = {}
        self._zone_dwell_start: Dict[Tuple, datetime] = {}
        self._last_dwell_emit: Dict[Tuple, datetime] = {}
        self._visitor_counter = 0

    def _new_visitor_id(self) -> str:
        self._visitor_counter += 1
        return f'VIS_{uuid.uuid4().hex[:6]}'

    def process_detections(self, detections: List[Detection], frame_time: datetime, camera_type: str, frame_height: int=1080) -> List[Dict]:
        events = []
        if camera_type == 'entry_exit':
            events.extend(self._process_entry_exit(detections, frame_time, frame_height))
        elif camera_type == 'main_floor':
            events.extend(self._process_floor(detections, frame_time))
        elif camera_type == 'billing':
            events.extend(self._process_billing(detections, frame_time))
        for det in detections:
            session = self._active.get(det.track_id)
            if session:
                self._cross_cam_seen[session.visitor_id] = {'camera_id': det.camera_id, 'time': frame_time}
        events.extend(self._handle_lost_tracks(frame_time))
        return events

    def _process_entry_exit(self, detections: List[Detection], ts: datetime, frame_height: int) -> List[Dict]:
        events = []
        for det in detections:
            if det.track_id in self._active:
                session = self._active[det.track_id]
                session.bbox_trajectory.append(det.bbox)
                session.last_seen = ts
                direction = self.dir_classifier.classify(session.bbox_trajectory, frame_height)
                if direction == 'EXIT' and session.is_active:
                    session.is_active = False
                    session.exit_time = ts
                    self.reid_engine.register_exit(session)
                    events.append(self._build_event(session, 'EXIT', ts, det))
                    logger.debug(f'EXIT: {session.visitor_id}')
            else:
                matched = self.reid_engine.match(det.bbox, ts, camera_id=det.camera_id)
                if matched:
                    matched.track_id = det.track_id
                    matched.is_active = True
                    matched.exit_time = None
                    matched.reentry_count += 1
                    matched.bbox_trajectory = [det.bbox]
                    matched.last_seen = ts
                    matched.entry_time = ts
                    self._active[det.track_id] = matched
                    events.append(self._build_event(matched, 'REENTRY', ts, det))
                    logger.info(f'REENTRY: {matched.visitor_id} (count={matched.reentry_count})')
                else:
                    session = VisitorSession(visitor_id=self._new_visitor_id(), track_id=det.track_id, camera_id=det.camera_id, store_id=self.store_id, entry_time=ts, last_seen=ts, bbox_trajectory=[det.bbox])
                    session.is_staff = self.staff_detector.classify(session)
                    self._active[det.track_id] = session
                    direction = self.dir_classifier.classify(session.bbox_trajectory, frame_height)
                    if direction != 'EXIT':
                        events.append(self._build_event(session, 'ENTRY', ts, det))
                        logger.debug(f'ENTRY: {session.visitor_id} (staff={session.is_staff})')
        return events

    def _process_floor(self, detections: List[Detection], ts: datetime) -> List[Dict]:
        events = []
        for det in detections:
            session = self._active.get(det.track_id)
            if not session:
                continue
            session.bbox_trajectory.append(det.bbox)
            session.last_seen = ts
            zone_id = self._pos_to_zone(det.bbox, det.camera_id)
            if zone_id:
                prev_zone = session.zone_history[-1]['zone_id'] if session.zone_history else None
                if zone_id != prev_zone:
                    if prev_zone:
                        events.append(self._build_event(session, 'ZONE_EXIT', ts, det, zone_id=prev_zone))
                    events.append(self._build_event(session, 'ZONE_ENTER', ts, det, zone_id=zone_id))
                    session.zone_history.append({'zone_id': zone_id, 'enter_time': ts})
                    self._zone_dwell_start[det.track_id, zone_id] = ts
                    self._last_dwell_emit[det.track_id, zone_id] = ts
                key = (det.track_id, zone_id)
                if key in self._zone_dwell_start:
                    elapsed = (ts - self._zone_dwell_start[key]).total_seconds()
                    last_emit = self._last_dwell_emit.get(key, self._zone_dwell_start[key])
                    since_last = (ts - last_emit).total_seconds()
                    if elapsed >= self.DWELL_EMIT_INTERVAL_S and since_last >= self.DWELL_EMIT_INTERVAL_S:
                        events.append(self._build_event(session, 'ZONE_DWELL', ts, det, zone_id=zone_id, dwell_ms=int(elapsed * 1000)))
                        self._last_dwell_emit[key] = ts
        return events

    def _process_billing(self, detections: List[Detection], ts: datetime) -> List[Dict]:
        events = []
        for det in detections:
            session = self._active.get(det.track_id)
            if not session or session.is_staff:
                continue
            session.last_seen = ts
            zone_id = self._pos_to_zone(det.bbox, det.camera_id)
            if zone_id in ('BILLING', 'BILLING_QUEUE'):
                queue_depth = sum((1 for s in self._active.values() if s.in_billing_zone and (not s.is_staff)))
                if not session.in_billing_zone:
                    session.in_billing_zone = True
                    session.billing_zone_entry = ts
                    if zone_id == 'BILLING_QUEUE' and queue_depth > 0:
                        events.append(self._build_event(session, 'BILLING_QUEUE_JOIN', ts, det, zone_id=zone_id, metadata={'queue_depth': queue_depth + 1}))
                    else:
                        events.append(self._build_event(session, 'ZONE_ENTER', ts, det, zone_id=zone_id))
            elif session.in_billing_zone:
                session.in_billing_zone = False
                dwell = int((ts - session.billing_zone_entry).total_seconds() * 1000) if session.billing_zone_entry else 0
                events.append(self._build_event(session, 'BILLING_QUEUE_ABANDON', ts, det, zone_id='BILLING_QUEUE', dwell_ms=dwell))
        return events

    def _pos_to_zone(self, bbox: BBox, camera_id: str) -> Optional[str]:
        cx = bbox.cx / 1920
        cy = bbox.cy / 1080
        if 'BILLING' in camera_id:
            return 'BILLING_QUEUE' if cy < 0.5 else 'BILLING'
        elif 'FLOOR' in camera_id:
            if cx < 0.2:
                return 'SKINCARE'
            elif cx < 0.4:
                return 'MAKEUP'
            elif cx < 0.6:
                return 'HAIRCARE'
            elif cx < 0.8:
                return 'FRAGRANCE'
            else:
                return 'PERSONAL_CARE'
        return None

    def _handle_lost_tracks(self, ts: datetime) -> List[Dict]:
        events = []
        to_remove = []
        for track_id, session in self._active.items():
            if session.last_seen is None:
                continue
            elapsed = (ts - session.last_seen).total_seconds()
            if elapsed > self.TRACK_LOST_TIMEOUT_S and session.is_active:
                session.is_active = False
                session.exit_time = ts
                self.reid_engine.register_exit(session)
                det_placeholder = Detection(track_id=track_id, bbox=session.bbox_trajectory[-1] if session.bbox_trajectory else BBox(0, 0, 1, 1), confidence=0.5, frame_idx=0, timestamp=ts, camera_id=session.camera_id)
                events.append(self._build_event(session, 'EXIT', ts, det_placeholder))
                to_remove.append(track_id)
                self._completed.append(session)
        for tid in to_remove:
            del self._active[tid]
        return events

    def _build_event(self, session: VisitorSession, event_type: str, ts: datetime, det: Detection, zone_id: Optional[str]=None, dwell_ms: int=0, metadata: Optional[Dict]=None) -> Dict:
        session.increment_seq()
        if metadata is None:
            metadata = {'queue_depth': None, 'sku_zone': self._get_sku_zone(zone_id), 'session_seq': session.session_seq}
        else:
            metadata.setdefault('sku_zone', self._get_sku_zone(zone_id))
            metadata.setdefault('session_seq', session.session_seq)
        return {'event_id': str(uuid.uuid4()), 'store_id': session.store_id, 'camera_id': det.camera_id, 'visitor_id': session.visitor_id, 'event_type': event_type, 'timestamp': ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 'zone_id': zone_id, 'dwell_ms': dwell_ms, 'is_staff': session.is_staff, 'confidence': round(det.confidence, 3), 'metadata': metadata}

    def _get_sku_zone(self, zone_id: Optional[str]) -> Optional[str]:
        sku_map = {'SKINCARE': 'MOISTURISER', 'MAKEUP': 'COSMETICS', 'HAIRCARE': 'HAIR', 'FRAGRANCE': 'FRAGRANCE', 'PERSONAL_CARE': 'PERSONAL_CARE'}
        return sku_map.get(zone_id)

    @property
    def active_sessions(self) -> Dict[int, VisitorSession]:
        return self._active

    @property
    def completed_sessions(self) -> List[VisitorSession]:
        return self._completed