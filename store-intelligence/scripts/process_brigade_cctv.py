import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / 'pipeline'))
sys.path.insert(0, str(ROOT / 'app'))
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(name)s: %(message)s')
logger = logging.getLogger('cctv_pipeline')
BRIGADE_STORE_OPEN_UTC = datetime(2026, 4, 10, 4, 30, 0, tzinfo=timezone.utc)
BRIGADE_STORE_CLOSE_UTC = datetime(2026, 4, 10, 16, 30, 0, tzinfo=timezone.utc)
BRIGADE_CAMERAS = [{'filename': 'CAM 1.mp4', 'camera_id': 'CAM_ENTRY_01', 'camera_type': 'entry_exit', 'description': 'Main entrance — entry/exit counting', 'clip_start': BRIGADE_STORE_OPEN_UTC}, {'filename': 'CAM 2.mp4', 'camera_id': 'CAM_FLOOR_01', 'camera_type': 'main_floor', 'description': 'Main floor — Skincare & Makeup zones', 'clip_start': BRIGADE_STORE_OPEN_UTC}, {'filename': 'CAM 3.mp4', 'camera_id': 'CAM_FLOOR_02', 'camera_type': 'main_floor', 'description': 'Main floor — Haircare & Fragrance zones', 'clip_start': BRIGADE_STORE_OPEN_UTC}, {'filename': 'CAM 4.mp4', 'camera_id': 'CAM_BILLING_01', 'camera_type': 'billing', 'description': 'Billing counter area', 'clip_start': BRIGADE_STORE_OPEN_UTC}, {'filename': 'CAM 5.mp4', 'camera_id': 'CAM_BILLING_02', 'camera_type': 'billing', 'description': 'Billing queue area', 'clip_start': BRIGADE_STORE_OPEN_UTC}]
STORE_ID = 'STORE_BLR_002'
MIN_CONFIDENCE = 0.35
MIN_CONFIDENCE_BILLING = 0.2
PERSON_CLASS = 0

def load_yolo(model_name: str='yolov8n.pt'):
    try:
        from ultralytics import YOLO
        model = YOLO(model_name)
        logger.info(f'YOLOv8 loaded: {model_name}')
        return model
    except ImportError:
        logger.error('ultralytics not installed: pip install ultralytics')
        return None
    except Exception as e:
        logger.error(f'YOLO load failed: {e}')
        return None

def get_video_info(cap, video_path: str):
    import cv2
    fps = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    duration_s = total_frames / fps if fps > 0 else 0
    logger.info(f'  Video: {width}x{height} @ {fps:.1f}fps | {total_frames} frames | {duration_s / 60:.1f} min')
    return (fps, total_frames, width, height)

def build_zone_map(camera_id: str, frame_width: int, frame_height: int):
    if 'FLOOR_01' in camera_id:
        return [{'zone_id': 'SKINCARE', 'x1': 0, 'y1': 0, 'x2': frame_width * 0.4, 'y2': frame_height, 'sku': 'MOISTURISER'}, {'zone_id': 'MAKEUP', 'x1': frame_width * 0.4, 'y1': 0, 'x2': frame_width, 'y2': frame_height, 'sku': 'COSMETICS'}]
    elif 'FLOOR_02' in camera_id or 'FLOOR' in camera_id:
        return [{'zone_id': 'HAIRCARE', 'x1': 0, 'y1': 0, 'x2': frame_width * 0.5, 'y2': frame_height, 'sku': 'HAIR'}, {'zone_id': 'PERSONAL_CARE', 'x1': frame_width * 0.5, 'y1': 0, 'x2': frame_width * 0.75, 'y2': frame_height, 'sku': 'PERSONAL_CARE'}, {'zone_id': 'FRAGRANCE', 'x1': frame_width * 0.75, 'y1': 0, 'x2': frame_width, 'y2': frame_height, 'sku': 'FRAGRANCE'}]
    elif 'BILLING' in camera_id:
        return [{'zone_id': 'BILLING_QUEUE', 'x1': 0, 'y1': 0, 'x2': frame_width, 'y2': frame_height * 0.6, 'sku': None}, {'zone_id': 'BILLING', 'x1': 0, 'y1': frame_height * 0.6, 'x2': frame_width, 'y2': frame_height, 'sku': None}]
    return []

def classify_zone(cx: float, cy: float, zones: list):
    for z in zones:
        if z['x1'] <= cx <= z['x2'] and z['y1'] <= cy <= z['y2']:
            return z
    return None

def make_event(event_type: str, camera_id: str, visitor_id: str, ts: datetime, zone_id=None, dwell_ms: int=0, is_staff: bool=False, confidence: float=0.88, seq: int=1, queue_depth=None, sku_zone=None) -> dict:
    return {'event_id': str(uuid.uuid4()), 'store_id': STORE_ID, 'camera_id': camera_id, 'visitor_id': visitor_id, 'event_type': event_type, 'timestamp': ts.strftime('%Y-%m-%dT%H:%M:%SZ'), 'zone_id': zone_id, 'dwell_ms': dwell_ms, 'is_staff': is_staff, 'confidence': round(confidence, 3), 'metadata': {'queue_depth': queue_depth, 'sku_zone': sku_zone, 'session_seq': seq}}

class VisitorState:

    def __init__(self, track_id: int, camera_id: str, ts: datetime):
        self.track_id = track_id
        self.visitor_id = f'VIS_{uuid.uuid4().hex[:6]}'
        self.camera_id = camera_id
        self.first_seen = ts
        self.last_seen = ts
        self.positions = []
        self.current_zone = None
        self.zone_enter_time = None
        self.last_dwell_emit = None
        self.events_emitted = []
        self.session_seq = 0
        self.is_staff = False

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq

def process_clip_real(clip_path: str, cam_config: dict, model, output_events: list, frame_skip: int=3) -> int:
    import cv2
    camera_id = cam_config['camera_id']
    camera_type = cam_config['camera_type']
    clip_start = cam_config['clip_start']
    cap = cv2.VideoCapture(clip_path)
    if not cap.isOpened():
        logger.error(f'Cannot open: {clip_path}')
        return 0
    fps, total_frames, frame_width, frame_height = get_video_info(cap, clip_path)
    zones = build_zone_map(camera_id, frame_width, frame_height)
    ENTRY_LINE_Y = frame_height * 0.4
    active_tracks: dict[int, VisitorState] = {}
    lost_tracks: dict[str, VisitorState] = {}
    REENTRY_WINDOW_S = 1800
    frame_idx = 0
    events_count = 0
    last_log = time.time()
    start_time = time.time()
    billing_queue_occupants: set = set()
    logger.info(f"  Starting detection on {cam_config['filename']} [{camera_type}]...")
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        frame_idx += 1
        if frame_idx % frame_skip != 0:
            continue
        offset_s = frame_idx / fps
        ts = clip_start + timedelta(seconds=offset_s)
        conf_threshold = MIN_CONFIDENCE_BILLING if camera_type == 'billing' else MIN_CONFIDENCE
        try:
            results = model.track(frame, classes=[PERSON_CLASS], conf=conf_threshold, tracker='bytetrack.yaml', persist=True, verbose=False)
        except Exception as e:
            logger.warning(f'  Frame {frame_idx}: inference error: {e}')
            continue
        current_tids = set()
        for r in results or []:
            if r.boxes is None:
                continue
            for i in range(len(r.boxes)):
                try:
                    x1, y1, x2, y2 = r.boxes.xyxy[i].tolist()
                    conf = float(r.boxes.conf[i])
                    tid_tensor = r.boxes.id
                    if tid_tensor is None:
                        continue
                    track_id = int(tid_tensor[i])
                    current_tids.add(track_id)
                    cx = (x1 + x2) / 2
                    cy = (y1 + y2) / 2
                    if track_id not in active_tracks:
                        reentry_vid = None
                        for vid, old_state in list(lost_tracks.items()):
                            elapsed = (ts - old_state.last_seen).total_seconds()
                            if elapsed <= REENTRY_WINDOW_S:
                                if old_state.camera_id == camera_id:
                                    reentry_vid = vid
                                    old_state.track_id = track_id
                                    active_tracks[track_id] = old_state
                                    del lost_tracks[vid]
                                    break
                        if track_id not in active_tracks:
                            state = VisitorState(track_id, camera_id, ts)
                            active_tracks[track_id] = state
                        else:
                            state = active_tracks[track_id]
                        if reentry_vid:
                            ev = make_event('REENTRY', camera_id, state.visitor_id, ts, confidence=conf, seq=state.next_seq())
                            output_events.append(ev)
                            events_count += 1
                        elif camera_type == 'entry_exit':
                            state.positions.append((cx, cy, ts))
                    state = active_tracks[track_id]
                    state.last_seen = ts
                    state.positions.append((cx, cy, ts))
                    if camera_type == 'entry_exit' and len(state.positions) >= 3:
                        prev_cy = state.positions[-3][1]
                        curr_cy = cy
                        if prev_cy < ENTRY_LINE_Y <= curr_cy and (not state.events_emitted):
                            ev = make_event('ENTRY', camera_id, state.visitor_id, ts, confidence=conf, seq=state.next_seq())
                            output_events.append(ev)
                            state.events_emitted.append('ENTRY')
                            events_count += 1
                    if camera_type in ('main_floor', 'billing') and zones:
                        detected_zone = classify_zone(cx, cy, zones)
                        zone_id = detected_zone['zone_id'] if detected_zone else None
                        if zone_id != state.current_zone:
                            if state.current_zone and state.zone_enter_time:
                                dwell_s = (ts - state.zone_enter_time).total_seconds()
                                if dwell_s >= 10:
                                    sku = None
                                    for z in zones:
                                        if z['zone_id'] == state.current_zone:
                                            sku = z.get('sku')
                                            break
                                    ev = make_event('ZONE_DWELL', camera_id, state.visitor_id, ts, zone_id=state.current_zone, dwell_ms=int(dwell_s * 1000), confidence=conf, seq=state.next_seq(), sku_zone=sku)
                                    output_events.append(ev)
                                    events_count += 1
                            if zone_id:
                                sku = detected_zone.get('sku') if detected_zone else None
                                ev = make_event('ZONE_ENTER', camera_id, state.visitor_id, ts, zone_id=zone_id, confidence=conf, seq=state.next_seq(), sku_zone=sku)
                                output_events.append(ev)
                                events_count += 1
                            state.current_zone = zone_id
                            state.zone_enter_time = ts if zone_id else None
                        if camera_type == 'billing':
                            if zone_id == 'BILLING_QUEUE':
                                if track_id not in billing_queue_occupants:
                                    billing_queue_occupants.add(track_id)
                                    queue_depth = len(billing_queue_occupants)
                                    ev = make_event('BILLING_QUEUE_JOIN', camera_id, state.visitor_id, ts, zone_id='BILLING_QUEUE', confidence=conf, seq=state.next_seq(), queue_depth=queue_depth)
                                    output_events.append(ev)
                                    events_count += 1
                            else:
                                billing_queue_occupants.discard(track_id)
                except Exception as e:
                    continue
        lost_tids = set(active_tracks.keys()) - current_tids
        for lost_tid in lost_tids:
            state = active_tracks.pop(lost_tid)
            elapsed_since_last = (ts - state.last_seen).total_seconds()
            if elapsed_since_last > 2.0:
                if camera_type == 'entry_exit' and 'ENTRY' in state.events_emitted:
                    if state.positions and state.positions[-1][1] < ENTRY_LINE_Y:
                        ev = make_event('EXIT', camera_id, state.visitor_id, state.last_seen, confidence=0.75, seq=state.next_seq())
                        output_events.append(ev)
                        events_count += 1
                if camera_type in ('main_floor', 'billing') and state.current_zone and state.zone_enter_time:
                    dwell_s = (state.last_seen - state.zone_enter_time).total_seconds()
                    if dwell_s >= 10:
                        ev = make_event('ZONE_DWELL', camera_id, state.visitor_id, state.last_seen, zone_id=state.current_zone, dwell_ms=int(dwell_s * 1000), confidence=0.75, seq=state.next_seq())
                        output_events.append(ev)
                        events_count += 1
                lost_tracks[state.visitor_id] = state
        now = time.time()
        if now - last_log > 15:
            pct = frame_idx / total_frames * 100 if total_frames > 0 else 0
            elapsed = now - start_time
            fps_proc = frame_idx / elapsed if elapsed > 0 else 0
            logger.info(f"  [{cam_config['filename']}] {frame_idx}/{total_frames} ({pct:.0f}%) | active_tracks={len(active_tracks)} | events={events_count} | processing @ {fps_proc:.0f}fps")
            last_log = now
    cap.release()
    final_ts = clip_start + timedelta(seconds=total_frames / fps)
    for state in active_tracks.values():
        if camera_type == 'entry_exit' and 'ENTRY' in state.events_emitted:
            if state.positions and state.positions[-1][1] > ENTRY_LINE_Y:
                ev = make_event('EXIT', camera_id, state.visitor_id, final_ts, confidence=0.7, seq=state.next_seq())
                output_events.append(ev)
                events_count += 1
        if camera_type in ('main_floor', 'billing') and state.current_zone and state.zone_enter_time:
            dwell_s = (final_ts - state.zone_enter_time).total_seconds()
            if dwell_s >= 10:
                ev = make_event('ZONE_DWELL', camera_id, state.visitor_id, final_ts, zone_id=state.current_zone, dwell_ms=int(dwell_s * 1000), confidence=0.7, seq=state.next_seq())
                output_events.append(ev)
                events_count += 1
    duration_s = time.time() - start_time
    logger.info(f"  ✓ {cam_config['filename']} complete: {events_count} events | {frame_idx} frames | {duration_s:.0f}s")
    return events_count

def post_to_api(events: list, api_url: str, batch_size: int=500) -> int:
    import urllib.request
    import urllib.error
    total_accepted = 0
    for i in range(0, len(events), batch_size):
        batch = events[i:i + batch_size]
        payload = json.dumps({'events': batch}).encode()
        req = urllib.request.Request(f'{api_url}/events/ingest', data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                result = json.loads(resp.read())
                accepted = result.get('accepted', 0)
                total_accepted += accepted
                logger.info(f"  Batch {i // batch_size + 1}: accepted={accepted}, duplicates={result.get('duplicates', 0)}, trace_id={result.get('trace_id', 'N/A')}")
        except urllib.error.URLError as e:
            logger.error(f'  API ingest failed: {e}')
    return total_accepted

def main():
    parser = argparse.ArgumentParser(description='Brigade Road CCTV Processing Pipeline — YOLOv8 + ByteTrack')
    parser.add_argument('--clips-dir', default=str(ROOT.parent / 'uploads' / 'CCTV Footage'), help='Directory containing CAM 1-5.mp4')
    parser.add_argument('--output', default=str(ROOT / 'data' / 'brigade_cctv_events.jsonl'), help='Output JSONL path for events')
    parser.add_argument('--api-url', default=None, help='API URL to POST events to')
    parser.add_argument('--frame-skip', type=int, default=3, help='Process every Nth frame (default: 3)')
    parser.add_argument('--model', default='yolov8n.pt', help='YOLO model (yolov8n.pt / yolov8s.pt)')
    parser.add_argument('--cameras', nargs='+', default=None, help="Only process specific cameras (e.g. 'CAM 1' 'CAM 2')")
    args = parser.parse_args()
    clips_dir = Path(args.clips_dir)
    logger.info('=' * 60)
    logger.info(' Brigade Road Store Intelligence — CCTV Pipeline')
    logger.info('=' * 60)
    logger.info(f' Clips dir: {clips_dir}')
    logger.info(f' Output:    {args.output}')
    logger.info(f' Model:     {args.model}')
    logger.info(f' Frame skip: {args.frame_skip} (process every {args.frame_skip}rd frame)')
    logger.info('=' * 60)
    if not clips_dir.exists():
        logger.error(f'Clips directory not found: {clips_dir}')
        logger.info('Expected location: ../uploads/CCTV Footage/CAM 1.mp4 ... CAM 5.mp4')
        sys.exit(1)
    model = load_yolo(args.model)
    if model is None:
        logger.error('Cannot load YOLO model. Exiting.')
        sys.exit(1)
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    all_events = []
    pipeline_start = time.time()
    for cam_config in BRIGADE_CAMERAS:
        clip_path = clips_dir / cam_config['filename']
        if args.cameras and cam_config['filename'].replace('.mp4', '') not in args.cameras:
            logger.info(f"Skipping {cam_config['filename']} (not in --cameras filter)")
            continue
        if not clip_path.exists():
            logger.warning(f'Clip not found: {clip_path}')
            continue
        size_mb = clip_path.stat().st_size / 1024 / 1024
        logger.info(f"\nProcessing: {cam_config['filename']} ({size_mb:.0f}MB)")
        logger.info(f"  Camera: {cam_config['camera_id']} | Type: {cam_config['camera_type']}")
        logger.info(f"  {cam_config['description']}")
        n = process_clip_real(str(clip_path), cam_config, model, all_events, frame_skip=args.frame_skip)
        logger.info(f"  → {n} events from {cam_config['filename']}\n")
    all_events.sort(key=lambda e: e['timestamp'])
    with open(args.output, 'w') as f:
        for ev in all_events:
            f.write(json.dumps(ev) + '\n')
    total_time = time.time() - pipeline_start
    logger.info('=' * 60)
    logger.info(f'Pipeline complete in {total_time / 60:.1f} minutes')
    logger.info(f'Total events: {len(all_events)}')
    logger.info(f'Output: {args.output}')
    logger.info('=' * 60)
    from collections import Counter
    type_counts = Counter((e['event_type'] for e in all_events))
    for et, cnt in sorted(type_counts.items()):
        logger.info(f'  {et}: {cnt}')
    if args.api_url and all_events:
        logger.info(f'\nPosting {len(all_events)} events to {args.api_url}...')
        accepted = post_to_api(all_events, args.api_url)
        logger.info(f'Total accepted by API: {accepted}')
    elif not args.api_url and all_events:
        logger.info(f'\nTo ingest into API:\n  1. Start API: python app/main.py\n  2. Run: python scripts/run_cctv_pipeline.py --api-url http://localhost:8000')
if __name__ == '__main__':
    main()