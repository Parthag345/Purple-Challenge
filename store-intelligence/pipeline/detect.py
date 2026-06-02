import argparse
import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Any
import numpy as np
from tracker import BBox, Detection, VisitorTracker, DirectionClassifier, StaffDetector
from emit import EventEmitter, make_event, EventType
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('detect')
YOLO_MODEL = 'yolov8n.pt'
PERSON_CLASS_ID = 0
MIN_CONFIDENCE = 0.35
FRAME_SKIP = 3
CAMERA_TYPES = {'CAM_ENTRY_01': 'entry_exit', 'CAM_FLOOR_01': 'main_floor', 'CAM_BILLING_01': 'billing'}
BRIGADE_CLIP_CAMERA_MAP = {'CAM 1': ('CAM_ENTRY_01', 'entry_exit'), 'CAM 2': ('CAM_FLOOR_01', 'main_floor'), 'CAM 3': ('CAM_FLOOR_01', 'main_floor'), 'CAM 4': ('CAM_BILLING_01', 'billing'), 'CAM 5': ('CAM_BILLING_01', 'billing')}
STORE_ID_MAP = {'ST1008': 'STORE_BLR_002', 'Brigade_Bangalore': 'STORE_BLR_002'}

class YOLODetector:

    def __init__(self, model_path: str=YOLO_MODEL, device: str='cpu'):
        self.model = None
        self.device = device
        self._model_path = model_path
        try:
            from ultralytics import YOLO
            self.model = YOLO(model_path)
            logger.info(f'YOLOv8 loaded: {model_path} on {device}')
        except ImportError:
            logger.warning('ultralytics not installed — using simulation mode')
        except Exception as e:
            logger.warning(f'YOLO load failed ({e}) — using simulation mode')

    def detect_and_track(self, frame, frame_idx: int, ts: datetime, camera_id: str) -> List['Detection']:
        if self.model is None:
            return []
        results = self.model.track(frame, classes=[PERSON_CLASS_ID], conf=MIN_CONFIDENCE, tracker='bytetrack.yaml', persist=True, verbose=False, device=self.device)
        detections = []
        for r in results:
            if r.boxes is None:
                continue
            boxes = r.boxes
            for i in range(len(boxes)):
                try:
                    x1, y1, x2, y2 = boxes.xyxy[i].tolist()
                    conf = float(boxes.conf[i])
                    tid = boxes.id
                    track_id = int(tid[i]) if tid is not None else frame_idx * 1000 + i
                    detections.append(Detection(track_id=track_id, bbox=BBox(x1, y1, x2, y2), confidence=conf, frame_idx=frame_idx, timestamp=ts, camera_id=camera_id, class_id=PERSON_CLASS_ID))
                except Exception:
                    continue
        return detections

    def detect(self, frame, frame_idx: int, ts: datetime, camera_id: str) -> List['Detection']:
        return self.detect_and_track(frame, frame_idx, ts, camera_id)

    @property
    def available(self) -> bool:
        return self.model is not None

def process_video_clip(video_path: str, store_id: str, camera_id: str, tracker: 'VisitorTracker', emitter: 'EventEmitter', clip_start_time: datetime, frame_skip: int=FRAME_SKIP, detector: Optional['YOLODetector']=None) -> int:
    try:
        import cv2
    except ImportError:
        logger.error('OpenCV not installed. Run: pip install opencv-python-headless')
        return 0
    if detector is None:
        detector = YOLODetector()
    if not detector.available:
        logger.warning(f'No YOLO model — skipping real video processing for {camera_id}')
        return 0
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        logger.error(f'Cannot open video: {video_path}')
        return 0
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    camera_type = CAMERA_TYPES.get(camera_id, 'main_floor')
    from tracker import ZoneClassifier
    zone_clf = ZoneClassifier({}, camera_id, frame_width, frame_height)
    logger.info(f'Processing {os.path.basename(video_path)} | {frame_width}x{frame_height} @ {fps:.1f}fps | {total_frames} frames | camera_type={camera_type}')
    frame_idx = 0
    processed = 0
    events_count = 0
    last_log_time = time.time()
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        if frame_idx % frame_skip != 0:
            continue
        processed += 1
        offset_s = frame_idx / fps
        ts = clip_start_time + timedelta(seconds=offset_s)
        detections = detector.detect_and_track(frame, frame_idx, ts, camera_id)
        events = tracker.process_detections(detections, ts, camera_type, frame_height=frame_height)
        for event in events:
            from emit import StoreEvent
            se = StoreEvent(**event)
            emitter.emit(se)
            events_count += 1
        now = time.time()
        if processed % 500 == 0 or now - last_log_time > 30:
            pct = frame_idx / total_frames * 100 if total_frames > 0 else 0
            logger.info(f'  [{camera_id}] {frame_idx}/{total_frames} ({pct:.0f}%) | detections/frame: {len(detections)} | events so far: {events_count}')
            last_log_time = now
    cap.release()
    logger.info(f'Clip done: {os.path.basename(video_path)} | {processed} frames processed | {events_count} events emitted')
    return events_count

class StoreSimulator:
    STORE_ID = 'STORE_BLR_002'
    STORE_OPEN = datetime(2026, 4, 10, 10, 0, 0, tzinfo=timezone.utc)
    STORE_CLOSE = datetime(2026, 4, 10, 22, 0, 0, tzinfo=timezone.utc)
    HOURLY_WEIGHTS = {10: 0.02, 11: 0.03, 12: 0.12, 13: 0.1, 14: 0.08, 15: 0.09, 16: 0.08, 17: 0.07, 18: 0.07, 19: 0.12, 20: 0.12, 21: 0.1}
    ZONE_PROBS = {'MAKEUP': 0.54, 'SKINCARE': 0.27, 'HAIRCARE': 0.06, 'PERSONAL_CARE': 0.04, 'FRAGRANCE': 0.01, 'BILLING': 0.08}
    STAFF_COUNT = 5
    TOTAL_CUSTOMERS = 35
    CONVERSION_RATE = 0.65

    def __init__(self, total_duration_minutes: int=720):
        self.duration = total_duration_minutes
        self.visitors: List[Dict] = []
        self._rng = random.Random(42)

    def generate_events(self) -> List[Dict]:
        events = []
        staff_events = self._generate_staff_events()
        events.extend(staff_events)
        customer_events = self._generate_customer_events()
        events.extend(customer_events)
        events.sort(key=lambda e: e['timestamp'])
        return events

    def _generate_staff_events(self) -> List[Dict]:
        events = []
        for i in range(self.STAFF_COUNT):
            vid = f'VIS_STAFF_{i:03d}'
            entry_time = self.STORE_OPEN + timedelta(minutes=self._rng.randint(0, 30))
            exit_time = self.STORE_CLOSE - timedelta(minutes=self._rng.randint(0, 20))
            events.append(self._make_event(vid, 'ENTRY', 'CAM_ENTRY_01', entry_time, is_staff=True, zone_id=None, seq=1))
            zone_visits = self._rng.randint(20, 40)
            zones = list(self.ZONE_PROBS.keys())
            current_time = entry_time + timedelta(minutes=5)
            for j in range(zone_visits):
                zone = self._rng.choice(zones)
                events.append(self._make_event(vid, 'ZONE_ENTER', 'CAM_FLOOR_01', current_time, is_staff=True, zone_id=zone, seq=j + 2))
                current_time += timedelta(minutes=self._rng.uniform(3, 15))
                if current_time >= exit_time:
                    break
            events.append(self._make_event(vid, 'EXIT', 'CAM_ENTRY_01', exit_time, is_staff=True, zone_id=None, seq=zone_visits + 2))
        return events

    def _generate_customer_events(self) -> List[Dict]:
        events = []
        total_minutes = (self.STORE_CLOSE - self.STORE_OPEN).seconds // 60
        arrival_times = self._sample_arrivals(self.TOTAL_CUSTOMERS)
        will_purchase = set(self._rng.sample(range(self.TOTAL_CUSTOMERS), int(self.TOTAL_CUSTOMERS * self.CONVERSION_RATE)))
        for i, arrival in enumerate(arrival_times):
            vid = f'VIS_{uuid.uuid4().hex[:6]}'
            group_size = 1
            if self._rng.random() < 0.25:
                group_size = self._rng.randint(2, 4)
            will_buy = i in will_purchase
            seq = 0

            def next_seq():
                nonlocal seq
                seq += 1
                return seq
            for member in range(group_size):
                member_vid = vid if member == 0 else f'VIS_{uuid.uuid4().hex[:6]}'
                entry_time = arrival + timedelta(seconds=member * self._rng.randint(0, 3))
                events.append(self._make_event(member_vid, 'ENTRY', 'CAM_ENTRY_01', entry_time, zone_id=None, seq=next_seq(), confidence=self._rng.uniform(0.75, 0.98)))
            current_time = arrival + timedelta(seconds=5)
            zones_to_visit = self._sample_zones(will_buy)
            for zone in zones_to_visit:
                camera = 'CAM_BILLING_01' if zone in ('BILLING', 'BILLING_QUEUE') else 'CAM_FLOOR_01'
                events.append(self._make_event(vid, 'ZONE_ENTER', camera, current_time, zone_id=zone, seq=next_seq(), confidence=self._rng.uniform(0.7, 0.96)))
                dwell_s = self._rng.uniform(30, 300)
                current_time += timedelta(seconds=dwell_s)
                if dwell_s >= 30:
                    events.append(self._make_event(vid, 'ZONE_DWELL', camera, current_time, zone_id=zone, dwell_ms=int(dwell_s * 1000), seq=next_seq(), confidence=self._rng.uniform(0.7, 0.96)))
                events.append(self._make_event(vid, 'ZONE_EXIT', camera, current_time, zone_id=zone, seq=next_seq(), confidence=self._rng.uniform(0.7, 0.96)))
            if will_buy:
                queue_depth = self._rng.randint(0, 4)
                events.append(self._make_event(vid, 'BILLING_QUEUE_JOIN', 'CAM_BILLING_01', current_time, zone_id='BILLING_QUEUE', seq=next_seq(), metadata={'queue_depth': queue_depth, 'sku_zone': None, 'session_seq': seq}, confidence=self._rng.uniform(0.8, 0.98)))
                current_time += timedelta(seconds=self._rng.uniform(60, 300))
            elif self._rng.random() < 0.3 and 'BILLING_QUEUE' in zones_to_visit:
                events.append(self._make_event(vid, 'BILLING_QUEUE_ABANDON', 'CAM_BILLING_01', current_time, zone_id='BILLING_QUEUE', seq=next_seq(), confidence=self._rng.uniform(0.65, 0.9)))
            if self._rng.random() < 0.1:
                reentry_time = current_time + timedelta(minutes=self._rng.randint(5, 25))
                if reentry_time < self.STORE_CLOSE:
                    events.append(self._make_event(vid, 'REENTRY', 'CAM_ENTRY_01', reentry_time, zone_id=None, seq=next_seq(), confidence=self._rng.uniform(0.7, 0.9)))
            exit_time = current_time + timedelta(minutes=self._rng.uniform(1, 10))
            exit_time = min(exit_time, self.STORE_CLOSE)
            events.append(self._make_event(vid, 'EXIT', 'CAM_ENTRY_01', exit_time, zone_id=None, seq=next_seq(), confidence=self._rng.uniform(0.75, 0.97)))
        return events

    def _sample_arrivals(self, n: int) -> List[datetime]:
        arrivals = []
        store_open_h = self.STORE_OPEN.hour
        for _ in range(n):
            hour_bucket = self._rng.choices(list(self.HOURLY_WEIGHTS.keys()), weights=list(self.HOURLY_WEIGHTS.values()))[0]
            minute = self._rng.randint(0, 59)
            second = self._rng.randint(0, 59)
            arrivals.append(self.STORE_OPEN.replace(hour=hour_bucket, minute=minute, second=second))
        return sorted(arrivals)

    def _sample_zones(self, will_purchase: bool) -> List[str]:
        zones = []
        product_zones = [z for z in self.ZONE_PROBS.keys() if z != 'BILLING']
        n_zones = self._rng.randint(1, 4)
        chosen = self._rng.choices(product_zones, weights=[self.ZONE_PROBS[z] / sum((self.ZONE_PROBS[z] for z in product_zones)) for z in product_zones], k=n_zones)
        zones.extend(chosen)
        if will_purchase:
            zones.extend(['BILLING_QUEUE', 'BILLING'])
        return zones

    def _make_event(self, visitor_id: str, event_type: str, camera_id: str, ts: datetime, zone_id: Optional[str]=None, dwell_ms: int=0, is_staff: bool=False, confidence: float=0.88, seq: int=1, metadata: Optional[Dict]=None) -> Dict:
        if metadata is None:
            metadata = {'queue_depth': None, 'sku_zone': self._zone_to_sku(zone_id), 'session_seq': seq}
        return {'event_id': str(uuid.uuid4()), 'store_id': self.STORE_ID, 'camera_id': camera_id, 'visitor_id': visitor_id, 'event_type': event_type, 'timestamp': ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 'zone_id': zone_id, 'dwell_ms': dwell_ms, 'is_staff': is_staff, 'confidence': round(confidence, 3), 'metadata': metadata}

    @staticmethod
    def _zone_to_sku(zone_id: Optional[str]) -> Optional[str]:
        sku_map = {'SKINCARE': 'MOISTURISER', 'MAKEUP': 'COSMETICS', 'HAIRCARE': 'HAIR', 'FRAGRANCE': 'FRAGRANCE', 'PERSONAL_CARE': 'PERSONAL_CARE'}
        return sku_map.get(zone_id)

def load_pos_transactions(csv_path: str) -> List[Dict]:
    import csv
    transactions = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            transactions.append({'store_id': row['store_id'], 'transaction_id': row['transaction_id'], 'timestamp': row['timestamp'], 'basket_value_inr': float(row['basket_value_inr'])})
    return transactions

def load_brigade_data(csv_path: str) -> List[Dict]:
    import csv
    transactions = []
    seen_orders = {}
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            oid = row.get('order_id') or row.get('Order ID', '').strip()
            if not oid:
                continue
            if oid not in seen_orders:
                order_date = row.get('order_date') or row.get('Order Date', '')
                order_time = row.get('order_time') or row.get('Order Time', '')
                try:
                    dt = datetime.strptime(f'{order_date.strip()} {order_time.strip()}', '%d-%m-%Y %H:%M:%S')
                    ts = dt.replace(tzinfo=timezone.utc)
                except ValueError:
                    try:
                        dt = datetime.strptime(order_date.strip(), '%Y-%m-%d')
                        ts = dt.replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
                raw_store = row.get('store_id') or row.get('store_name', '')
                api_store_id = STORE_ID_MAP.get(raw_store, 'STORE_BLR_002')
                seen_orders[oid] = {'store_id': api_store_id, 'transaction_id': f'TXN_{oid}', 'timestamp': ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 'basket_value_inr': 0.0}
            nmv = float(row.get('NMV', 0) or 0)
            if nmv > 0:
                seen_orders[oid]['basket_value_inr'] += nmv
    result = list(seen_orders.values())
    logger.info(f'Loaded {len(result)} Brigade POS orders from {csv_path}')
    return result

def main():
    parser = argparse.ArgumentParser(description='Store Intelligence Detection Pipeline')
    parser.add_argument('--video', type=str, default=None, help='Path to video clips directory')
    parser.add_argument('--store', type=str, default='STORE_BLR_002', help='Store ID')
    parser.add_argument('--output', type=str, default='data/events.jsonl', help='Output JSONL path')
    parser.add_argument('--simulate', action='store_true', help='Use simulation mode')
    parser.add_argument('--brigade-csv', type=str, default=None, help='Brigade POS CSV for simulation calibration')
    parser.add_argument('--post-to-api', action='store_true', help='POST events to API after generation')
    parser.add_argument('--api-url', type=str, default='http://localhost:8000', help='API base URL')
    parser.add_argument('--clip-start', type=str, default='2026-04-10T10:00:00Z', help='Clip start time ISO-8601')
    args = parser.parse_args()
    os.makedirs(os.path.dirname(args.output) if os.path.dirname(args.output) else '.', exist_ok=True)
    clip_start = datetime.strptime(args.clip_start, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    if args.simulate or not args.video:
        logger.info('Running in SIMULATION mode')
        simulator = StoreSimulator()
        events = simulator.generate_events()
        logger.info(f'Generated {len(events)} events')
        with open(args.output, 'w') as f:
            for event in events:
                f.write(json.dumps(event) + '\n')
        logger.info(f'Events written to {args.output}')
        if args.post_to_api:
            _post_events_to_api(events, args.api_url)
        return
    logger.info(f'Processing video clips from: {args.video}')
    store_layout = {}
    layout_path = Path(args.video).parent / 'store_layout.json'
    if layout_path.exists():
        with open(layout_path) as f:
            store_layout = json.load(f)
    tracker = VisitorTracker(args.store, store_layout)
    total_events = 0
    with EventEmitter(args.output) as emitter:
        video_dir = Path(args.video)
        clips = sorted(video_dir.glob('*.mp4')) + sorted(video_dir.glob('*.avi'))
        if not clips:
            logger.warning(f'No video clips found in {args.video}')
            return
        for clip_path in clips:
            filename = clip_path.stem
            filename_upper = filename.upper()
            camera_id = 'CAM_FLOOR_01'
            camera_type_override = None
            for brigade_key, (cam_id, cam_type) in BRIGADE_CLIP_CAMERA_MAP.items():
                if filename == brigade_key or filename_upper == brigade_key.upper():
                    camera_id = cam_id
                    camera_type_override = cam_type
                    break
            if camera_type_override is None:
                if 'ENTRY' in filename_upper:
                    camera_id = 'CAM_ENTRY_01'
                elif 'BILLING' in filename_upper or 'BILL' in filename_upper:
                    camera_id = 'CAM_BILLING_01'
                else:
                    camera_id = 'CAM_FLOOR_01'
            n = process_video_clip(str(clip_path), args.store, camera_id, tracker, emitter, clip_start)
            total_events += n
            logger.info(f'Clip {clip_path.name}: {n} events')
    logger.info(f'Total events emitted: {total_events}')
    if args.post_to_api:
        with open(args.output) as f:
            events = [json.loads(line) for line in f if line.strip()]
        _post_events_to_api(events, args.api_url)

def _post_events_to_api(events: List[Dict], api_url: str, batch_size: int=500):
    import urllib.request
    import urllib.error
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        payload = json.dumps({'events': batch}).encode()
        req = urllib.request.Request(f'{api_url}/events/ingest', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                logger.info(f'Ingested batch {i // batch_size + 1}: {result}')
        except urllib.error.URLError as e:
            logger.error(f'API ingest failed: {e}')
if __name__ == '__main__':
    main()