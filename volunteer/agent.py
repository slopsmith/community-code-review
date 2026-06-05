"""
Volunteer WebSocket Agent — connects to coordinator for inference.

Connects to the coordinator via a persistent outbound WebSocket tunnel,
receives inference requests, sends them to the local llama-server via HTTP,
and returns results. This agent runs alongside llama-server in the container.

Every connection is outbound-initiated — works behind NAT, CGNAT, firewalls.

v2: GPU-aware scheduling with automatic state reporting.
"""

import asyncio
import json
import logging
import os
import subprocess
import urllib.parse

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("volunteer-agent")

# ── Configuration from environment ───────────────────────────────────────
COORDINATOR_URL = os.environ["COORDINATOR_URL"]
VOLUNTEER_ID = os.environ.get("VOLUNTEER_ID", f"{os.uname().nodename}-{os.getpid()}")
VOLUNTEER_SECRET = os.environ.get("VOLUNTEER_SECRET", "")
LLAMA_PORT = int(os.environ.get("LLAMA_PORT", "8080"))
GPU_INFO = os.environ.get("GPU_INFO", "unknown")
MODEL_FILE = os.environ.get("MODEL_FILE", "unknown")

# GPU thresholds (match coordinator defaults)
GPU_UTIL_THRESHOLD = int(os.environ.get("GPU_UTIL_THRESHOLD", "70"))
GPU_MEM_THRESHOLD = int(os.environ.get("GPU_MEM_THRESHOLD", "85"))

# Derive WebSocket URL from the coordinator URL
WS_BASE = COORDINATOR_URL.replace("http://", "ws://").replace("https://", "wss://")
WS_URL = f"{WS_BASE}/ws/{VOLUNTEER_ID}"

if VOLUNTEER_SECRET:
    WS_URL += f"?secret={urllib.parse.quote(VOLUNTEER_SECRET, safe='')}"

LLAMA_API_URL = f"http://localhost:{LLAMA_PORT}/v1/chat/completions"

# Protocol version
PROTOCOL_VERSION = 2

# Shared state for GPU monitoring
_gpu_stats = {"utilization": 0, "memory": 0}
_active_requests = 0
_current_state = "ready"
_state_lock = asyncio.Lock()


async def get_gpu_stats() -> dict:
    """Poll GPU utilization using vendor-specific tools."""
    stats = {"utilization": 0, "memory": 0}

    # Try NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            line = result.stdout.strip().split("\n")[0]
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 3:
                stats["utilization"] = int(float(parts[0]))
                mem_used = float(parts[1])
                mem_total = float(parts[2])
                if mem_total > 0:
                    stats["memory"] = int((mem_used / mem_total) * 100)
                return stats
    except Exception:
        pass

    # Try AMD (rocm-smi)
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            # Parse rocm-smi output (simplified)
            lines = result.stdout.strip().split("\n")
            for line in lines:
                if "GPU use" in line.lower():
                    parts = line.split()
                    for i, p in enumerate(parts):
                        if "%" in p:
                            stats["utilization"] = int(float(p.replace("%", "")))
                if "vram" in line.lower() and "%" in line:
                    parts = line.split()
                    for p in parts:
                        if "%" in p:
                            stats["memory"] = int(float(p.replace("%", "")))
            return stats
    except Exception:
        pass

    # Try Intel (intel_gpu_top)
    try:
        result = subprocess.run(
            ["intel_gpu_top", "-s", "1000", "-l", "1"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                if "render" in line.lower():
                    parts = line.split()
                    for p in parts:
                        if "%" in p:
                            stats["utilization"] = int(float(p.replace("%", "")))
                            break
            return stats
    except Exception:
        pass

    return stats


async def gpu_monitor_task(ws):
    """Poll GPU every 5 seconds and send status to coordinator."""
    global _gpu_stats
    while True:
        await asyncio.sleep(5)
        try:
            stats = await get_gpu_stats()
            _gpu_stats = stats

            await ws.send(json.dumps({
                "type": "gpu_status",
                "utilization_percent": stats["utilization"],
                "memory_used_percent": stats["memory"],
            }))

            # Log user-friendly messages
            if stats["utilization"] > GPU_UTIL_THRESHOLD:
                logger.info("🎮 GPU busy — utilization at %d%% (gaming or other workload detected)", stats["utilization"])
            elif stats["utilization"] < 10:
                logger.debug("GPU quiet — utilization at %d%%", stats["utilization"])

            # Update model state based on GPU + active requests
            await _update_model_state(ws)

        except Exception as e:
            logger.warning("GPU monitor error: %s", e)
            break


async def _update_model_state(ws):
    """Update and broadcast model state based on GPU and request load."""
    global _current_state
    async with _state_lock:
        old_state = _current_state

        if _active_requests > 0:
            new_state = "busy"
        elif _gpu_stats["utilization"] > GPU_UTIL_THRESHOLD or _gpu_stats["memory"] > GPU_MEM_THRESHOLD:
            new_state = "busy"
        else:
            new_state = "ready"

        if new_state != old_state:
            _current_state = new_state
            await ws.send(json.dumps({
                "type": "model_state",
                "state": new_state,
            }))
            logger.info("📊 Model state: %s → %s", old_state, new_state)


async def run_agent():
    """Main loop: connect WebSocket, handle messages, reconnect on drop."""
    while True:
        try:
            await connect_and_serve()
        except Exception as e:
            logger.error("Connection error: %s — reconnecting in 10s...", e)
            await asyncio.sleep(10)


async def connect_and_serve():
    """Connect to the coordinator via persistent WebSocket and process messages."""
    import websockets

    logger.info("Connecting to coordinator at %s", WS_URL)

    async with websockets.connect(WS_URL, ping_interval=None) as ws:
        # ── Step 1: Send init message with GPU metadata ──────────────────
        init_msg = {
            "type": "init",
            "protocol_version": PROTOCOL_VERSION,
            "gpu_info": GPU_INFO,
            "model": MODEL_FILE,
        }
        await ws.send(json.dumps(init_msg))
        logger.info("Sent init message to coordinator (protocol v%d)", PROTOCOL_VERSION)

        # ── Step 2: Start background tasks ───────────────────────────────
        heartbeat_task = asyncio.create_task(send_heartbeats(ws))
        gpu_task = asyncio.create_task(gpu_monitor_task(ws))

        try:
            # ── Step 3: Message loop ────────────────────────────────────
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("Invalid JSON from coordinator: %s", raw[:200])
                    continue

                msg_type = data.get("type")

                if msg_type == "inference":
                    req_id = data.get("id")
                    body = data.get("body", {})
                    logger.info(
                        "📥 Received a code review request — running inference..."
                    )
                    # Fire and forget — handle concurrently
                    asyncio.create_task(
                        handle_inference(ws, req_id, body)
                    )

                elif msg_type == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

                else:
                    logger.debug("Ignoring message type: %s", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket closed by coordinator")
        finally:
            heartbeat_task.cancel()
            gpu_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            try:
                await gpu_task
            except asyncio.CancelledError:
                pass


async def send_heartbeats(ws):
    """Send a heartbeat every 30 seconds."""
    while True:
        await asyncio.sleep(30)
        try:
            await ws.send(json.dumps({"type": "heartbeat"}))
            logger.debug("Heartbeat sent")
        except Exception as e:
            logger.warning("Heartbeat failed: %s", e)
            break


async def handle_inference(ws, req_id: str, body: dict):
    """
    Forward an inference request to the local llama-server and send
    the result back through the WebSocket.
    """
    global _active_requests
    async with _state_lock:
        _active_requests += 1
        await _update_model_state(ws)

    logger.info("Forwarding request %s to llama-server...", req_id)

    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                LLAMA_API_URL,
                json=body,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            result = response.json()

        # Send success response
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "success",
            "body": result,
        }))
        logger.info("✅ Review complete — result sent back to coordinator")

    except httpx.TimeoutException:
        logger.error("Request %s timed out against llama-server", req_id)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": "llama-server timed out",
        }))
    except httpx.RequestError as e:
        logger.error("Request %s failed against llama-server: %s", req_id, e)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": str(e),
        }))
    except Exception as e:
        logger.error("Unexpected error on request %s: %s", req_id, e)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": str(e),
        }))
    finally:
        async with _state_lock:
            _active_requests = max(0, _active_requests - 1)
            await _update_model_state(ws)


if __name__ == "__main__":
    logger.info("Starting volunteer WebSocket agent...")
    logger.info("  Volunteer ID:  %s", VOLUNTEER_ID)
    logger.info("  Coordinator:   %s", COORDINATOR_URL)
    logger.info("  GPU:           %s", GPU_INFO)
    logger.info("  Model:         %s", MODEL_FILE)
    logger.info("  Protocol:      v%d", PROTOCOL_VERSION)

    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Shutting down")
