import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
logging.basicConfig(level=logging.INFO, format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "msg": %(message)s}')
logger = logging.getLogger('api')
from database import check_db_health, get_db, init_db
from ingestion import ingest_events, ingest_pos_transactions
from metrics import get_store_metrics
from funnel import get_funnel, get_heatmap
from anomalies import get_anomalies
from health import get_health
from models import AnomaliesResponse, ErrorResponse, FunnelResponse, HealthResponse, HeatmapResponse, IngestRequest, IngestResponse, StoreMetrics

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('"Starting Store Intelligence API"')
    try:
        init_db()
        _load_pos_data_on_startup()
        logger.info('"Database initialized"')
    except Exception as e:
        logger.error(f'"DB init failed: {e}"')
    yield
    logger.info('"Shutting down"')

def _load_pos_data_on_startup():
    import csv, os
    pos_files = ['/data/pos_transactions.csv', 'data/pos_transactions.csv']
    brigade_files = ['/data/brigade_pos.csv', 'data/brigade_pos.csv', '../uploads/Brigade_Bangalore_10_April_26 (1)bc6219c.csv', 'uploads/Brigade_Bangalore_10_April_26 (1)bc6219c.csv']
    for fpath in pos_files:
        if os.path.exists(fpath):
            try:
                txns = []
                with open(fpath) as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        txns.append({'transaction_id': row['transaction_id'], 'store_id': row['store_id'], 'timestamp': row['timestamp'], 'basket_value_inr': float(row['basket_value_inr'])})
                n = ingest_pos_transactions(txns)
                logger.info(f'"Loaded {n} POS transactions from {fpath}"')
            except Exception as e:
                logger.warning(f'"POS load failed for {fpath}: {e}"')
    brigade_csv_loaded = False
    for fpath in brigade_files:
        if os.path.exists(fpath) and (not brigade_csv_loaded):
            try:
                import sys
                import os as _os
                pipeline_dir = _os.path.join(_os.path.dirname(__file__), '..', 'pipeline')
                if pipeline_dir not in sys.path:
                    sys.path.insert(0, pipeline_dir)
                from detect import load_brigade_data
                txns = load_brigade_data(fpath)
                n = ingest_pos_transactions(txns)
                logger.info(f'"Loaded {n} Brigade POS transactions from {fpath}"')
                brigade_csv_loaded = True
            except Exception as e:
                logger.warning(f'"Brigade POS load failed for {fpath}: {e}"')
app = FastAPI(title='Store Intelligence API', description='Real-time analytics API for offline retail stores — Purplle Tech Challenge 2026', version='1.0.0', lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])

@app.middleware('http')
async def structured_logging_middleware(request: Request, call_next):
    trace_id = str(uuid.uuid4())
    request.state.trace_id = trace_id
    start = time.time()
    store_id = request.path_params.get('store_id', 'N/A')
    endpoint = request.url.path
    try:
        response = await call_next(request)
        latency_ms = round((time.time() - start) * 1000, 1)
        logger.info(f'"trace_id": "{trace_id}", "method": "{request.method}", "endpoint": "{endpoint}", "store_id": "{store_id}", "latency_ms": {latency_ms}, "status_code": {response.status_code}')
        response.headers['X-Trace-ID'] = trace_id
        return response
    except Exception as e:
        latency_ms = round((time.time() - start) * 1000, 1)
        logger.error(f'"trace_id": "{trace_id}", "endpoint": "{endpoint}", "latency_ms": {latency_ms}, "error": "{str(e)}"')
        return JSONResponse(status_code=500, content=_error_body('INTERNAL_ERROR', str(e), trace_id), headers={'X-Trace-ID': trace_id})

def _require_db(trace_id: str):
    if not check_db_health():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=_error_body('DATABASE_UNAVAILABLE', 'Database is currently unavailable. Please retry shortly.', trace_id))

def _error_body(code: str, message: str, trace_id: str) -> Dict:
    return {'error': code, 'detail': [{'code': code, 'message': message}], 'trace_id': trace_id, 'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}

def _get_trace(request: Request) -> str:
    return getattr(request.state, 'trace_id', str(uuid.uuid4()))

@app.post('/events/ingest', response_model=IngestResponse, status_code=200, summary='Ingest a batch of CCTV events', tags=['Events'])
async def post_events_ingest(body: IngestRequest, request: Request):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        result = ingest_events(body.events, trace_id)
        logger.info(f'"trace_id": "{trace_id}", "event": "ingest", "event_count": {len(body.events)}, "accepted": {result.accepted}, "duplicates": {result.duplicates}, "invalid": {result.invalid}')
        return result
    except Exception as e:
        logger.error(f'"trace_id": "{trace_id}", "ingest_error": "{e}"')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_error_body('INGEST_ERROR', 'Event ingestion failed', trace_id))

@app.get('/stores/{store_id}/metrics', response_model=StoreMetrics, summary='Real-time store metrics', tags=['Analytics'])
async def get_metrics(store_id: str, request: Request):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        metrics = get_store_metrics(store_id)
        if metrics is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_error_body('STORE_NOT_FOUND', f'No data for store {store_id}', trace_id))
        return metrics
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f'"trace_id": "{trace_id}", "metrics_error": "{e}"')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_error_body('METRICS_ERROR', 'Metrics computation failed', trace_id))

@app.get('/stores/{store_id}/funnel', response_model=FunnelResponse, summary='Conversion funnel', tags=['Analytics'])
async def get_funnel_endpoint(store_id: str, request: Request, window_hours: int=Query(24, ge=1, le=168, description='Lookback window in hours')):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        return get_funnel(store_id, window_hours)
    except Exception as e:
        logger.error(f'"trace_id": "{trace_id}", "funnel_error": "{e}"')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_error_body('FUNNEL_ERROR', 'Funnel computation failed', trace_id))

@app.get('/stores/{store_id}/heatmap', summary='Zone visit heatmap', tags=['Analytics'])
async def get_heatmap_endpoint(store_id: str, request: Request):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        return get_heatmap(store_id)
    except Exception as e:
        logger.error(f'"trace_id": "{trace_id}", "heatmap_error": "{e}"')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_error_body('HEATMAP_ERROR', 'Heatmap computation failed', trace_id))

@app.get('/stores/{store_id}/anomalies', response_model=AnomaliesResponse, summary='Active anomalies', tags=['Analytics'])
async def get_anomalies_endpoint(store_id: str, request: Request):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        return get_anomalies(store_id)
    except Exception as e:
        logger.error(f'"trace_id": "{trace_id}", "anomalies_error": "{e}"')
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=_error_body('ANOMALIES_ERROR', 'Anomaly detection failed', trace_id))

@app.get('/health', response_model=HealthResponse, summary='Service health', tags=['Infrastructure'])
async def get_health_endpoint():
    try:
        return get_health()
    except Exception as e:
        logger.error(f'"health_error": "{e}"')
        return JSONResponse(status_code=503, content={'status': 'unhealthy', 'error': str(e), 'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')})

@app.post('/pos/ingest', summary='Ingest POS transactions', tags=['Events'])
async def post_pos_ingest(transactions: List[Dict], request: Request):
    trace_id = _get_trace(request)
    _require_db(trace_id)
    try:
        n = ingest_pos_transactions(transactions)
        return {'ingested': n, 'trace_id': trace_id}
    except Exception as e:
        raise HTTPException(500, detail=_error_body('POS_ERROR', str(e), trace_id))

@app.get('/', tags=['Infrastructure'])
async def root():
    return {'service': 'Store Intelligence API', 'version': '1.0.0', 'docs': '/docs', 'health': '/health'}

@app.exception_handler(404)
async def not_found_handler(request: Request, exc):
    trace_id = _get_trace(request)
    return JSONResponse(status_code=404, content=_error_body('NOT_FOUND', f'Endpoint not found: {request.url.path}', trace_id), headers={'X-Trace-ID': trace_id})

@app.exception_handler(422)
async def validation_handler(request: Request, exc):
    trace_id = _get_trace(request)
    errors = []
    if hasattr(exc, 'errors'):
        for err in exc.errors():
            errors.append({'code': 'VALIDATION_ERROR', 'message': err.get('msg', 'Validation failed'), 'field': '.'.join((str(loc) for loc in err.get('loc', [])))})
    return JSONResponse(status_code=422, content={'error': 'VALIDATION_ERROR', 'detail': errors or [{'code': 'VALIDATION_ERROR', 'message': 'Invalid request'}], 'trace_id': trace_id, 'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}, headers={'X-Trace-ID': trace_id})
if __name__ == '__main__':
    uvicorn.run('main:app', host='0.0.0.0', port=int(os.getenv('PORT', 8000)), reload=os.getenv('ENV', 'production') == 'development', log_level='info')