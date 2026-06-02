#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# run.sh — One command to process all clips and feed events into the API
# Usage:
#   ./pipeline/run.sh                           # simulate events + ingest
#   ./pipeline/run.sh --video /data/clips/      # real video + ingest
#   ./pipeline/run.sh --simulate --no-api       # simulate only, no API call
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUTPUT_FILE="${ROOT_DIR}/data/events.jsonl"
API_URL="${API_URL:-http://localhost:8000}"
VIDEO_DIR="${VIDEO_DIR:-}"
SIMULATE=false
NO_API=false
BRIGADE_CSV="${ROOT_DIR}/data/brigade_pos.csv"

# Parse args
while [[ $# -gt 0 ]]; do
    case $1 in
        --video) VIDEO_DIR="$2"; shift 2 ;;
        --simulate) SIMULATE=true; shift ;;
        --no-api) NO_API=true; shift ;;
        --api-url) API_URL="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; shift ;;
    esac
done

echo "═══════════════════════════════════════════════════"
echo " Store Intelligence — Detection Pipeline"
echo " Output: ${OUTPUT_FILE}"
echo " API: ${API_URL}"
echo "═══════════════════════════════════════════════════"

# Ensure data dir
mkdir -p "${ROOT_DIR}/data"

# Wait for API to be ready (if posting)
if [[ "$NO_API" == "false" ]]; then
    echo "⏳ Waiting for API..."
    for i in $(seq 1 30); do
        if curl -sf "${API_URL}/health" > /dev/null 2>&1; then
            echo "✅ API ready"
            break
        fi
        sleep 2
    done
fi

# Run detection
cd "${SCRIPT_DIR}"

if [[ "$SIMULATE" == "true" ]] || [[ -z "$VIDEO_DIR" ]]; then
    echo "🎭 Running in SIMULATION mode..."
    python3 detect.py \
        --simulate \
        --store STORE_BLR_002 \
        --output "${OUTPUT_FILE}" \
        ${NO_API:+} \
        $([ "$NO_API" == "false" ] && echo "--post-to-api --api-url ${API_URL}" || echo "")
else
    echo "🎥 Processing real video clips from: ${VIDEO_DIR}"
    python3 detect.py \
        --video "${VIDEO_DIR}" \
        --store STORE_BLR_002 \
        --output "${OUTPUT_FILE}" \
        $([ "$NO_API" == "false" ] && echo "--post-to-api --api-url ${API_URL}" || echo "")
fi

EVENT_COUNT=$(wc -l < "${OUTPUT_FILE}" || echo 0)
echo ""
echo "✅ Pipeline complete"
echo "   Events generated: ${EVENT_COUNT}"
echo "   Output file: ${OUTPUT_FILE}"
