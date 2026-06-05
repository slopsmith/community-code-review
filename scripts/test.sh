#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
#  Integration Test — Community Code Review
#
#  Deterministic, headless test that verifies the coordinator ↔ volunteer
#  pipeline end-to-end.  Designed to run:
#    • Locally during development
#    • By automated agents
#    • In GitHub Actions CI
#
#  MOCK_MODE=1 (default) skips model download and starts a lightweight
#  HTTP stub instead of llama-server.  This means the test works on
#  machines with limited VRAM (e.g. RTX 2060 8 GB) and in CI runners
#  that have no GPU at all.
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# ── Configuration ─────────────────────────────────────────────────────────
COORDINATOR_PORT="${COORDINATOR_PORT:-18080}"   # use non-default port to avoid conflicts
COORDINATOR_SECRET="${COORDINATOR_SECRET:-test-secret-$(openssl rand -hex 8)}"
MOCK_MODE="${MOCK_MODE:-1}"
VOLUNTEER_IMAGE="${VOLUNTEER_IMAGE:-volunteer:test}"
POLL_INTERVAL=2
POLL_MAX=60                                   # 2 min total timeout

# Containers get deterministic names so we can clean up easily
COORDINATOR_NAME="ccr-test-coordinator"
VOLUNTEER_NAME="ccr-test-volunteer"
NETWORK_NAME="ccr-test-net"

# ── Helpers ───────────────────────────────────────────────────────────────
info()  { echo "   [INFO] $*"; }
ok()    { echo "   [ OK ] $*"; }
warn()  { echo "   [WARN] $*"; }
fail()  { echo "   [FAIL] $*"; exit 1; }

cleanup() {
    info "Cleaning up test containers..."
    docker rm -f "${VOLUNTEER_NAME}" "${COORDINATOR_NAME}" >/dev/null 2>&1 || true
    docker network rm "${NETWORK_NAME}" >/dev/null 2>&1 || true
}

# Clean up any leftovers from previous aborted runs
cleanup

trap cleanup EXIT

# ── Step 1: Build coordinator image ───────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Community Code Review — Integration Test                    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
info "MOCK_MODE=${MOCK_MODE}  (1=stub llama-server, 0=real model)"
echo ""

info "Building coordinator image..."
docker build -t coordinator:test "${PROJECT_ROOT}/coordinator" >/dev/null
ok "Coordinator image built"

# ── Step 2: Build volunteer image ─────────────────────────────────────────
info "Building volunteer image..."
docker build \
    --build-arg ORG_NAME=slopsmith \
    -t "${VOLUNTEER_IMAGE}" \
    "${PROJECT_ROOT}/volunteer" >/dev/null
ok "Volunteer image built"

# ── Step 3: Create isolated Docker network ────────────────────────────────
info "Creating test network..."
docker network create "${NETWORK_NAME}" >/dev/null 2>&1 || true
ok "Network ready"

# ── Step 4: Start coordinator ─────────────────────────────────────────────
info "Starting coordinator on port ${COORDINATOR_PORT}..."
docker run -d \
    --name "${COORDINATOR_NAME}" \
    --network "${NETWORK_NAME}" \
    -p "${COORDINATOR_PORT}:8080" \
    -e COORDINATOR_SECRET="${COORDINATOR_SECRET}" \
    -e COORDINATOR_PORT=8080 \
    coordinator:test >/dev/null

# Wait for coordinator HTTP health endpoint
for i in $(seq 1 30); do
    if curl -sf "http://localhost:${COORDINATOR_PORT}/health" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

COORDINATOR_HEALTH=$(curl -sf "http://localhost:${COORDINATOR_PORT}/health" 2>/dev/null || echo "{}")
if ! echo "${COORDINATOR_HEALTH}" | grep -q '"status"'; then
    fail "Coordinator failed to start"
fi
ok "Coordinator healthy"

# ── Step 5: Start volunteer (mock mode) ───────────────────────────────────
info "Starting volunteer (connecting to coordinator)..."
docker run -d \
    --name "${VOLUNTEER_NAME}" \
    --network "${NETWORK_NAME}" \
    -e COORDINATOR_URL="http://${COORDINATOR_NAME}:8080" \
    -e VOLUNTEER_ID="test-volunteer" \
    -e VOLUNTEER_SECRET="${COORDINATOR_SECRET}" \
    -e MOCK_MODE="${MOCK_MODE}" \
    -e MODEL_REPO="Qwen/Qwen3-30B-A3B-GGUF" \
    -e MODEL_FILE="Qwen3-30B-A3B-Q4_K_M.gguf" \
    -e GPU_UTIL_THRESHOLD=70 \
    -e GPU_MEM_THRESHOLD=85 \
    "${VOLUNTEER_IMAGE}" >/dev/null

# ── Step 6: Wait for volunteer registration ───────────────────────────────
info "Waiting for volunteer to register..."
REGISTERED=false
for i in $(seq 1 ${POLL_MAX}); do
    # Abort early if volunteer container dies
    VOL_STATUS=$(docker inspect "${VOLUNTEER_NAME}" --format '{{.State.Status}}' 2>/dev/null || echo "gone")
    if [ "${VOL_STATUS}" != "running" ]; then
        fail "Volunteer container stopped (${VOL_STATUS}) — check: docker logs ${VOLUNTEER_NAME}"
    fi

    VOLUNTEERS=$(curl -sf "http://localhost:${COORDINATOR_PORT}/volunteers" 2>/dev/null || echo "[]")
    if echo "${VOLUNTEERS}" | grep -q '"test-volunteer"'; then
        REGISTERED=true
        break
    fi
    sleep ${POLL_INTERVAL}
done

if [ "${REGISTERED}" != true ]; then
    fail "Volunteer did not register within $((POLL_MAX * POLL_INTERVAL))s"
fi
ok "Volunteer registered"

# ── Step 7: Verify volunteer metadata ─────────────────────────────────────
info "Checking volunteer metadata..."
VOLUNTEERS=$(curl -sf "http://localhost:${COORDINATOR_PORT}/volunteers" 2>/dev/null || echo "[]")

# Extract the test-volunteer entry
VOL_JSON=$(echo "${VOLUNTEERS}" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for v in data:
    if v.get('id') == 'test-volunteer':
        print(json.dumps(v))
        sys.exit(0)
print('{}')
" 2>/dev/null || echo "{}")

if [ "${VOL_JSON}" = "{}" ]; then
    fail "Could not extract volunteer metadata"
fi

# Check protocol version
PROTO_VER=$(echo "${VOL_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('protocol_version','MISSING'))" 2>/dev/null || echo "MISSING")
if [ "${PROTO_VER}" = "MISSING" ]; then
    warn "protocol_version missing (v1 client — backward compat OK)"
else
    ok "protocol_version=${PROTO_VER}"
fi

# Check model_state
STATE=$(echo "${VOL_JSON}" | python3 -c "import json,sys; print(json.load(sys.stdin).get('model_state','MISSING'))" 2>/dev/null || echo "MISSING")
if [ "${STATE}" = "MISSING" ]; then
    warn "model_state missing (v1 client — backward compat OK)"
else
    ok "model_state=${STATE}"
fi

# ── Step 8: Send inference request ────────────────────────────────────────
info "Sending test inference request..."
RESPONSE=$(curl -sf "http://localhost:${COORDINATOR_PORT}/v1/chat/completions" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer ${COORDINATOR_SECRET}" \
    -d '{
        "model": "test-model",
        "messages": [{"role": "user", "content": "Say hello"}]
    }' 2>/dev/null || echo "{}")

if ! echo "${RESPONSE}" | grep -q '"choices"'; then
    echo "  Response: ${RESPONSE}"
    fail "Inference request failed"
fi
ok "Inference request succeeded"

# ── Step 9: Verify response content (mock mode only) ──────────────────────
if [ "${MOCK_MODE}" = "1" ]; then
    if echo "${RESPONSE}" | grep -q "MOCK REVIEW"; then
        ok "Mock response received"
    else
        warn "Expected mock response marker not found"
    fi
fi

# ── Step 10: Final report ─────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ✅ ALL TESTS PASSED                                         ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
info "Coordinator:  http://localhost:${COORDINATOR_PORT}"
info "Volunteer:    ${VOLUNTEER_NAME}"
info "Mock mode:    ${MOCK_MODE}"
echo ""
