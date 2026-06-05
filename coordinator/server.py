"""
Coordinator — OpenAI-compatible API relay that load-balances
inference requests across a pool of registered volunteers via WebSocket tunnels.

Architecture:
  Volunteers open a persistent OUTBOUND WebSocket connection to the coordinator.
  The coordinator never initiates TCP connections to volunteers — it sends work
  through the already-established WebSocket tunnel. This works through NAT,
  CGNAT, firewalls, and home routers.

  Both the HTTP API and WebSocket endpoint run on the same port (8080).

Environment variables:
  COORDINATOR_SECRET   — Shared secret volunteers present during WebSocket handshake
                         (optional; if not set, any volunteer can join)
  COORDINATOR_PORT     — Port to listen on (default: 8080)
"""

import asyncio
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

# ── Configuration ──────────────────────────────────────────────────────────
COORDINATOR_SECRET = os.environ.get("COORDINATOR_SECRET", "")
COORDINATOR_PORT = int(os.environ.get("COORDINATOR_PORT", "8080"))
STALE_VOLUNTEER_SECONDS = 90  # evict if no heartbeat for this long

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("coordinator")

try:
    _gpu_util = int(os.environ.get("GPU_UTIL_THRESHOLD", "70"))
    GPU_UTIL_THRESHOLD = max(0, min(100, _gpu_util))
except (ValueError, TypeError):
    logger.warning("Invalid GPU_UTIL_THRESHOLD, using default 70")
    GPU_UTIL_THRESHOLD = 70

try:
    _gpu_mem = int(os.environ.get("GPU_MEM_THRESHOLD", "85"))
    GPU_MEM_THRESHOLD = max(0, min(100, _gpu_mem))
except (ValueError, TypeError):
    logger.warning("Invalid GPU_MEM_THRESHOLD, using default 85")
    GPU_MEM_THRESHOLD = 85

app = FastAPI(title="CCR Coordinator")  # Community Code Review

# ── Volunteer registry ────────────────────────────────────────────────────
# volunteers[volunteer_id] = {
#     "websocket": WebSocket,
#     "gpu_info": str,
#     "model": str,
#     "model_state": str,        # unloaded, loading, ready, busy
#     "gpu_utilization_percent": int,
#     "gpu_memory_used_percent": int,
#     "max_parallel": int,
#     "load_duration_seconds": float,
#     "last_heartbeat": float,
#     "active_requests": int,
#     "registered_at": float,
#     "peer_addr": str,
# }
volunteers: dict[str, dict] = OrderedDict()
_vol_lock = asyncio.Lock()

# ── Pending request map ──────────────────────────────────────────────────
# Maps a request_id to an asyncio.Future that resolves with the result
_pending_requests: dict[str, asyncio.Future] = {}
_pending_lock = asyncio.Lock()


async def _evict_stale_volunteers():
    """Background: disconnect volunteers that miss their heartbeat."""
    while True:
        await asyncio.sleep(15)
        async with _vol_lock:
            now = time.time()
            stale = [
                vid
                for vid, v in volunteers.items()
                if now - v["last_heartbeat"] > STALE_VOLUNTEER_SECONDS
            ]
            for vid in stale:
                logger.warning("Evicting stale volunteer %s", vid)
                ws = volunteers[vid]["websocket"]
                del volunteers[vid]
                try:
                    await ws.close(code=1000, reason="Heartbeat timeout")
                except Exception:
                    pass
            if stale:
                logger.info("Pool now has %d volunteer(s)", len(volunteers))


async def _pick_volunteer() -> Optional[tuple[str, dict]]:
    """Pick the best volunteer based on state, GPU availability, and load."""
    async with _vol_lock:
        if not volunteers:
            return None

        # Filter out volunteers that are loading or have busy GPUs
        available = []
        for vid, v in volunteers.items():
            state = v.get("model_state", "ready")  # v1 clients default to ready
            util = v.get("gpu_utilization_percent", 0)
            mem = v.get("gpu_memory_used_percent", 0)
            max_p = v.get("max_parallel", 1)
            active = v.get("active_requests", 0)

            # Skip loading volunteers; keep unloaded for fallback
            if state == "loading":
                continue

            # Skip GPU-busy volunteers (v2+ only; v1 clients bypass this)
            proto_ver = v.get("protocol_version", 1)
            try:
                proto_ver = int(proto_ver)
            except (ValueError, TypeError):
                proto_ver = 1
            if proto_ver >= 2:
                # Clamp and validate GPU metrics before using them
                util = max(0, min(100, util))
                mem = max(0, min(100, mem))
                if util > GPU_UTIL_THRESHOLD or mem > GPU_MEM_THRESHOLD:
                    continue

            # Skip at-capacity volunteers
            if active >= max_p:
                continue

            available.append((vid, v))

        if not available:
            return None

        # Prefer ready volunteers, then unloaded, then busy with capacity
        def sort_key(item):
            vid, v = item
            state = v.get("model_state", "ready")
            active = v.get("active_requests", 0)
            # ready first (0), then busy (1), then unloaded (2)
            state_order = {"ready": 0, "busy": 1, "unloaded": 2}.get(state, 3)
            return (state_order, active)

        available.sort(key=sort_key)
        best_vid, v = available[0]
        v["active_requests"] += 1
        logger.info("Assigned request to %s (state=%s, load=%d/%d)",
                    best_vid, v.get("model_state", "ready"),
                    v["active_requests"], v.get("max_parallel", 1))
        return best_vid, v


async def _release_volunteer(vid: str):
    async with _vol_lock:
        if vid in volunteers:
            volunteers[vid]["active_requests"] = max(
                0, volunteers[vid]["active_requests"] - 1
            )


def _check_auth(request: Request):
    """If COORDINATOR_SECRET is set, validate Authorization header."""
    if not COORDINATOR_SECRET:
        return
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {COORDINATOR_SECRET}":
        raise HTTPException(status_code=403, detail="Invalid or missing auth token")


# ── WebSocket handler (volunteers connect here) ──────────────────────────

@app.websocket("/ws/{volunteer_id}")
async def websocket_endpoint(ws: WebSocket, volunteer_id: str):
    """
    Volunteers maintain a persistent WebSocket connection here.
    The coordinator sends inference requests through this tunnel.
    """
    # Check secret via query parameter
    if COORDINATOR_SECRET:
        token = ws.query_params.get("secret", "")
        if token != COORDINATOR_SECRET:
            await ws.close(code=4001, reason="Invalid volunteer secret")
            return

    await ws.accept()

    # Get volunteer metadata from the first message
    try:
        init_msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await ws.close(code=4002, reason="Expected init message")
        return

    if init_msg.get("type") != "init":
        await ws.close(code=4002, reason="First message must be type 'init'")
        return

    gpu_info = init_msg.get("gpu_info", "unknown")
    model = init_msg.get("model", "unknown")
    peer_addr = ws.client.host if ws.client else "unknown"
    protocol_version = init_msg.get("protocol_version", 1)
    # Coerce protocol_version to int for safe comparisons
    try:
        protocol_version = int(protocol_version)
    except (ValueError, TypeError):
        protocol_version = 1
    init_model_state = init_msg.get("model_state", "ready" if protocol_version == 1 else "unloaded")

    async with _vol_lock:
        volunteers[volunteer_id] = {
            "websocket": ws,
            "gpu_info": gpu_info,
            "model": model,
            "protocol_version": protocol_version,
            "model_state": init_model_state,
            "gpu_utilization_percent": 0,
            "gpu_memory_used_percent": 0,
            "max_parallel": 1,
            "load_duration_seconds": 0,
            "last_heartbeat": time.time(),
            "active_requests": 0,
            "registered_at": time.time(),
            "peer_addr": peer_addr,
        }

    if protocol_version < 2:
        logger.warning(
            "Volunteer %s is using protocol v%d (outdated). "
            "Please update your volunteer image for GPU-aware scheduling. "
            "Run: docker pull ghcr.io/slopsmith/volunteer:latest",
            volunteer_id, protocol_version
        )
    else:
        logger.info(
            "Volunteer connected via WebSocket: %s [%s] model=%s from %s pool=%d (protocol v%d)",
            volunteer_id, gpu_info, model, peer_addr, len(volunteers), protocol_version
        )

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type")

            if msg_type == "heartbeat":
                async with _vol_lock:
                    if volunteer_id in volunteers:
                        volunteers[volunteer_id]["last_heartbeat"] = time.time()

            elif msg_type == "gpu_status":
                # v2+ clients report GPU utilization
                async with _vol_lock:
                    if volunteer_id in volunteers:
                        volunteers[volunteer_id]["gpu_utilization_percent"] = data.get("utilization_percent", 0)
                        volunteers[volunteer_id]["gpu_memory_used_percent"] = data.get("memory_used_percent", 0)

            elif msg_type == "model_state":
                # v2+ clients report model load/unload state
                async with _vol_lock:
                    if volunteer_id in volunteers:
                        old_state = volunteers[volunteer_id].get("model_state", "unloaded")
                        new_state = data.get("state", old_state)
                        volunteers[volunteer_id]["model_state"] = new_state
                        if "load_duration_seconds" in data:
                            volunteers[volunteer_id]["load_duration_seconds"] = data["load_duration_seconds"]
                        if "max_parallel" in data:
                            volunteers[volunteer_id]["max_parallel"] = data["max_parallel"]
                        if old_state != new_state:
                            logger.info("Volunteer %s model state: %s → %s", volunteer_id, old_state, new_state)

            elif msg_type == "inference_result":
                req_id = data.get("id")
                async with _pending_lock:
                    future = _pending_requests.pop(req_id, None)
                if future and not future.done():
                    if data.get("status") == "error":
                        future.set_exception(
                            RuntimeError(data.get("error", "Unknown volunteer error"))
                        )
                    else:
                        future.set_result(data.get("body"))
                else:
                    logger.warning("Orphaned result for request %s", req_id)

                await _release_volunteer(volunteer_id)

            else:
                logger.warning("Unknown message type from %s: %s", volunteer_id, msg_type)

    except WebSocketDisconnect:
        logger.info("Volunteer disconnected: %s", volunteer_id)
    except Exception as e:
        logger.error("WebSocket error for %s: %s", volunteer_id, e)
    finally:
        async with _vol_lock:
            volunteers.pop(volunteer_id, None)
        logger.info("Volunteer removed: %s (pool=%d)", volunteer_id, len(volunteers))


# ── OpenAPI-compatible inference endpoint ────────────────────────────────

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.
    Relays the request through a volunteer's WebSocket tunnel.
    """
    _check_auth(request)
    body = await request.json()

    picked = await _pick_volunteer()
    if not picked:
        raise HTTPException(
            status_code=503,
            detail="No volunteers connected. Ask someone to run a volunteer container!",
        )

    vid, vinfo = picked
    req_id = str(uuid.uuid4())

    # Create a future to wait for the result
    future = asyncio.get_event_loop().create_future()
    async with _pending_lock:
        _pending_requests[req_id] = future

    try:
        # Send inference request through the WebSocket
        await vinfo["websocket"].send_json({
            "type": "inference",
            "id": req_id,
            "body": body,
        })

        # Wait for the result (with a generous timeout)
        try:
            result = await asyncio.wait_for(future, timeout=300.0)
            return JSONResponse(content=result)
        except asyncio.TimeoutError:
            logger.error("Volunteer %s timed out on request %s", vid, req_id)
            raise HTTPException(status_code=504, detail="Volunteer timed out")
        except RuntimeError as e:
            logger.error("Volunteer %s error on request %s: %s", vid, req_id, e)
            raise HTTPException(status_code=502, detail=str(e))

    finally:
        async with _pending_lock:
            _pending_requests.pop(req_id, None)
        await _release_volunteer(vid)


@app.get("/volunteers")
async def list_volunteers():
    """Admin endpoint — see current pool state."""
    async with _vol_lock:
        now = time.time()
        return [
            {
                "id": vid,
                "gpu_info": v["gpu_info"],
                "model": v["model"],
                "protocol_version": v.get("protocol_version", 1),
                "model_state": v.get("model_state", "ready"),
                "gpu_utilization_percent": v.get("gpu_utilization_percent", 0),
                "gpu_memory_used_percent": v.get("gpu_memory_used_percent", 0),
                "max_parallel": v.get("max_parallel", 1),
                "active_requests": v["active_requests"],
                "uptime_seconds": int(now - v["registered_at"]),
                "last_heartbeat_seconds_ago": int(now - v["last_heartbeat"]),
                "peer_addr": v["peer_addr"],
            }
            for vid, v in volunteers.items()
        ]


@app.get("/health")
async def health():
    """Simple health check."""
    async with _vol_lock:
        count = len(volunteers)
    return {"status": "healthy", "volunteer_count": count}


# ── Main ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    logger.info("Starting coordinator on port %d...", COORDINATOR_PORT)

    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=COORDINATOR_PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)
    server.run()