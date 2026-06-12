"""
Volunteer WebSocket Agent — connects to coordinator for inference.

Connects to the coordinator via a persistent outbound WebSocket tunnel,
receives inference requests, sends them to the local llama-server via HTTP,
and returns results. This agent runs alongside llama-server in the container.

Every connection is outbound-initiated — works behind NAT, CGNAT, firewalls.

v2: GPU-aware scheduling with automatic state reporting.
v3: Real model lifecycle — llama-server is started/stopped on demand to free
    VRAM when GPU is busy with other workloads or idle for too long.
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
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
MODEL_PATH = os.environ.get("MODEL_PATH", f"/models/{MODEL_FILE}")
MOCK_MODE = os.environ.get("MOCK_MODE", "0") == "1"

# GPU thresholds (match coordinator defaults)
try:
    GPU_UTIL_THRESHOLD = int(os.environ.get("GPU_UTIL_THRESHOLD", "70"))
except (ValueError, TypeError):
    logger.warning("Invalid GPU_UTIL_THRESHOLD, using default 70")
    GPU_UTIL_THRESHOLD = 70

try:
    GPU_MEM_THRESHOLD = int(os.environ.get("GPU_MEM_THRESHOLD", "85"))
except (ValueError, TypeError):
    logger.warning("Invalid GPU_MEM_THRESHOLD, using default 85")
    GPU_MEM_THRESHOLD = 85

# Idle timeout: unload model after this many seconds of inactivity
try:
    IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "120"))  # 2 minutes default
except (ValueError, TypeError):
    logger.warning("Invalid IDLE_TIMEOUT, using default 120")
    IDLE_TIMEOUT = 120

# llama-server configuration
try:
    LLAMA_CTX_SIZE = int(os.environ.get("LLAMA_CTX_SIZE", "32768"))
except (ValueError, TypeError):
    LLAMA_CTX_SIZE = 32768
try:
    LLAMA_N_PARALLEL = int(os.environ.get("LLAMA_N_PARALLEL", "1"))
except (ValueError, TypeError):
    LLAMA_N_PARALLEL = 1
try:
    LLAMA_TEMP = float(os.environ.get("LLAMA_TEMP", "0.7"))
except (ValueError, TypeError):
    LLAMA_TEMP = 0.7
LLAMA_N_GPU_LAYERS = os.environ.get("LLAMA_N_GPU_LAYERS", "")

# Derive WebSocket URL from the coordinator URL
WS_BASE = COORDINATOR_URL.replace("http://", "ws://").replace("https://", "wss://")
WS_URL = f"{WS_BASE}/ws/{VOLUNTEER_ID}"

if VOLUNTEER_SECRET:
    WS_URL += f"?secret={urllib.parse.quote(VOLUNTEER_SECRET, safe='')}"

LLAMA_API_URL = f"http://localhost:{LLAMA_PORT}/v1/chat/completions"
LLAMA_HEALTH_URL = f"http://localhost:{LLAMA_PORT}/health"

# Protocol version
PROTOCOL_VERSION = 3

# Shared state for GPU monitoring
_gpu_stats = {"utilization": 0, "memory": 0}
_active_requests = 0
_current_state = "unloaded"
_state_lock = asyncio.Lock()
_llama_process: asyncio.subprocess.Process | None = None
_llama_start_lock = asyncio.Lock()
_last_request_time = 0.0


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
                try:
                    stats["utilization"] = int(float(parts[0]))
                    mem_used = float(parts[1])
                    mem_total = float(parts[2])
                    if mem_total > 0:
                        stats["memory"] = int((mem_used / mem_total) * 100)
                    return stats
                except (ValueError, IndexError) as e:
                    logger.warning("Unexpected nvidia-smi output format: %s", e)
    except FileNotFoundError:
        pass  # nvidia-smi not installed
    except Exception as e:
        logger.warning("nvidia-smi probe failed: %s", e)

    # Try AMD (rocm-smi)
    try:
        result = subprocess.run(
            ["rocm-smi", "--showuse", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line_lower = line.lower()
                if "gpu use" in line_lower:
                    parts = line.split()
                    for _i, p in enumerate(parts):
                        if "%" in p:
                            try:
                                stats["utilization"] = int(float(p.replace("%", "")))
                            except ValueError:
                                logger.warning("Unexpected rocm-smi utilization format: %s", p)
                if "vram" in line_lower and "%" in line:
                    parts = line.split()
                    for p in parts:
                        if "%" in p:
                            try:
                                stats["memory"] = int(float(p.replace("%", "")))
                            except ValueError:
                                logger.warning("Unexpected rocm-smi memory format: %s", p)
            return stats
    except FileNotFoundError:
        pass  # rocm-smi not installed
    except Exception as e:
        logger.warning("rocm-smi probe failed: %s", e)

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
                            try:
                                stats["utilization"] = int(float(p.replace("%", "")))
                            except ValueError:
                                logger.warning("Unexpected intel_gpu_top utilization format: %s", p)
                            break
            return stats
    except FileNotFoundError:
        pass  # intel_gpu_top not installed
    except Exception as e:
        logger.warning("intel_gpu_top probe failed: %s", e)

    return stats


# ── llama-server lifecycle ──────────────────────────────────────────────


async def _is_llama_running() -> bool:
    """Check if llama-server (or mock server) is alive and serving."""
    global _llama_process
    if MOCK_MODE:
        # In mock mode, just probe the health endpoint
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(LLAMA_HEALTH_URL)
                return resp.status_code == 200
        except (httpx.RequestError, httpx.TimeoutException):
            return False
    if _llama_process is None:
        return False
    if _llama_process.returncode is not None:
        _llama_process = None
        return False
    # Also probe the HTTP health endpoint to confirm it's serving
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(LLAMA_HEALTH_URL)
            return resp.status_code == 200
    except (httpx.RequestError, httpx.TimeoutException):
        return False


async def _start_llama_server() -> bool:
    """Start llama-server subprocess and wait for it to be healthy."""
    global _llama_process

    if MOCK_MODE:
        # In mock mode, the entrypoint already started the mock server.
        # Just verify it's healthy.
        logger.info("🌀 Checking mock server health...")
        for _ in range(30):
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(LLAMA_HEALTH_URL)
                    if resp.status_code == 200:
                        logger.info("✅ Mock server ready")
                        return True
            except (httpx.RequestError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)
        logger.error("Mock server not healthy within 30s")
        return False

    async with _llama_start_lock:
        # Double-check after acquiring lock
        if await _is_llama_running():
            return True

        logger.info("🌀 Starting llama-server...")
        cmd = ["/app/llama-server",
               "-m", MODEL_PATH,
               "--host", "0.0.0.0",
               "--port", str(LLAMA_PORT),
               "-c", str(LLAMA_CTX_SIZE),
               "-np", str(LLAMA_N_PARALLEL),
               "--temp", str(LLAMA_TEMP),
               "--no-ui",
               "--no-warmup",
               "--no-mmap"]
        if LLAMA_N_GPU_LAYERS:
            cmd.extend(["-ngl", LLAMA_N_GPU_LAYERS])

        try:
            _llama_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            logger.error("llama-server binary not found at /app/llama-server")
            return False
        except Exception as e:
            logger.error("Failed to start llama-server: %s", e)
            return False

        # Wait for health endpoint (up to 5 minutes for model load)
        start_time = time.time()
        deadline = start_time + 300
        while time.time() < deadline:
            if _llama_process.returncode is not None:
                logger.error("llama-server exited prematurely (code %d)",
                             _llama_process.returncode)
                return False
            try:
                async with httpx.AsyncClient(timeout=2.0) as client:
                    resp = await client.get(LLAMA_HEALTH_URL)
                    if resp.status_code == 200:
                        elapsed = time.time() - start_time
                        logger.info("✅ llama-server ready in %.1fs", elapsed)
                        return True
            except (httpx.RequestError, httpx.TimeoutException):
                pass
            await asyncio.sleep(1)

        logger.error("llama-server did not become healthy within 300s — aborting")
        # Kill directly without going through _stop_llama_server to avoid
        # re-acquiring _llama_start_lock (which we already hold).
        proc = _llama_process
        _llama_process = None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        return False


async def _stop_llama_server() -> None:
    """Gracefully stop llama-server, freeing VRAM."""
    global _llama_process

    if MOCK_MODE:
        # In mock mode, the entrypoint manages the mock server
        logger.debug("Mock mode — not stopping mock server")
        return

    async with _llama_start_lock:
        proc = _llama_process
        _llama_process = None

        if proc is None or proc.returncode is not None:
            return  # already stopped

        logger.info("🛑 Stopping llama-server to free VRAM...")
        # Try graceful SIGTERM first
        try:
            proc.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10.0)
                logger.info("llama-server stopped gracefully")
                return
            except asyncio.TimeoutError:
                pass
            # Force kill if still alive
            proc.kill()
            await proc.wait()
            logger.info("llama-server killed after graceful timeout")
        except ProcessLookupError:
            pass  # already dead


# ── GPU monitoring + state machine ──────────────────────────────────────


async def _broadcast_model_state(ws, new_state: str, **extra):
    """Send a model_state message to the coordinator."""
    payload = {"type": "model_state", "state": new_state}
    payload.update(extra)
    await ws.send(json.dumps(payload))


async def _load_model(ws) -> bool:
    """Transition unloaded → loading → ready. Returns True on success."""
    global _current_state
    old_state = _current_state

    await _broadcast_model_state(ws, "loading")
    _current_state = "loading"
    logger.info("📊 Model state: %s → loading", old_state)

    success = await _start_llama_server()
    if success:
        elapsed = int(time.time() - _last_request_time)
        _current_state = "ready"
        await _broadcast_model_state(ws, "ready", load_duration_seconds=elapsed, max_parallel=LLAMA_N_PARALLEL)
        logger.info("📊 Model state: loading → ready")
        return True
    else:
        _current_state = "unloaded"
        await _broadcast_model_state(ws, "unloaded")
        logger.info("📊 Model state: loading → unloaded (load failed)")
        return False


async def _unload_model(ws) -> None:
    """Transition current state → unloaded, stopping llama-server."""
    global _current_state
    old_state = _current_state

    await _broadcast_model_state(ws, "unloading")
    _current_state = "unloading"

    await _stop_llama_server()

    _current_state = "unloaded"
    await _broadcast_model_state(ws, "unloaded")
    logger.info("📊 Model state: %s → unloaded", old_state)


async def _update_model_state(ws):
    """Update and broadcast model state based on GPU and request load.
    Must be called while holding _state_lock.

    Real state machine:
      unloaded  ──(inference arrives)──▶  loading  ──(llama-srv up)──▶  ready
      ready     ──(GPU busy)────────────▶  unloading ──(llama-srv down)▶ unloaded
      ready     ──(idle timeout)────────▶  unloading ──(llama-srv down)▶ unloaded
      ready/busy──(inference done)──────▶  ready (if no more requests)
      busy      ──(last request done)──▶  ready (still loaded)
      any       ──(GPU busy + no reqs)─▶  unloading ──▶ unloaded
    """
    global _current_state
    old_state = _current_state
    llama_running = await _is_llama_running()

    if _active_requests > 0:
        # If model isn't running, start it (first request triggers load)
        if not llama_running:
            await _load_model(ws)
            # Re-check if load succeeded
            if not await _is_llama_running():
                return
        # Update to busy since we have active requests
        if _current_state != "busy":
            _current_state = "busy"
            await _broadcast_model_state(ws, "busy")
            if old_state != "busy":
                logger.info("📊 Model state: %s → busy", old_state)
        return

    # No active requests — check if we should stay loaded or unload
    gpu_busy = (
        _gpu_stats["utilization"] > GPU_UTIL_THRESHOLD
        or _gpu_stats["memory"] > GPU_MEM_THRESHOLD
    )

    if gpu_busy and llama_running:
        # GPU is busy with another workload — unload to free VRAM
        await _unload_model(ws)
        return

    if llama_running:
        # Model is loaded, no requests, GPU is free — stay ready
        if old_state != "ready":
            _current_state = "ready"
            await _broadcast_model_state(ws, "ready", max_parallel=LLAMA_N_PARALLEL)
            logger.info("📊 Model state: %s → ready", old_state)
    else:
        # Model not running, no requests — stay unloaded
        if old_state != "unloaded":
            _current_state = "unloaded"
            await _broadcast_model_state(ws, "unloaded")
            logger.info("📊 Model state: %s → unloaded", old_state)


async def _update_model_state_safe(ws):
    """Acquire lock and update model state."""
    async with _state_lock:
        await _update_model_state(ws)


async def idle_timeout_task(ws):
    """Periodically check if the model should be unloaded due to inactivity."""
    global _last_request_time
    while True:
        await asyncio.sleep(15)
        async with _state_lock:
            if _active_requests > 0:
                continue
            if _current_state != "ready":
                continue
            idle_seconds = time.time() - _last_request_time
            if idle_seconds >= IDLE_TIMEOUT:
                llama_running = await _is_llama_running()
                if llama_running:
                    logger.info("⏰ Idle timeout reached (%.0fs) — unloading model",
                                idle_seconds)
                    await _unload_model(ws)


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
            await _update_model_state_safe(ws)

        except Exception as e:
            logger.warning("GPU monitor error: %s", e)
            break


async def run_agent():
    """Main loop: connect WebSocket, handle messages, reconnect on drop."""
    while True:
        try:
            await connect_and_serve()
        except Exception as e:
            logger.error("Connection error: %s — reconnecting in 10s...", e)
        logger.info("🔁 Reconnecting in 10 seconds...")
        await asyncio.sleep(10)


async def connect_and_serve():
    """Connect to the coordinator via persistent WebSocket and process messages."""
    import websockets

    logger.info("Connecting to coordinator at %s", WS_URL)

    # Seed GPU state before sending init so we report accurately
    global _gpu_stats
    try:
        _gpu_stats = await get_gpu_stats()
        logger.info("Initial GPU stats: utilization=%d%%, memory=%d%%",
                    _gpu_stats["utilization"], _gpu_stats["memory"])
    except Exception as e:
        logger.warning("Could not probe GPU before init: %s", e)

    # Try to connect with a timeout - if coordinator is not available, skip
    ws = None
    try:
        async with websockets.connect(WS_URL, ping_interval=None, open_timeout=5) as ws:
            # ── Step 1: Send init message with GPU metadata ──────────────────
            init_msg = {
                "type": "init",
                "protocol_version": PROTOCOL_VERSION,
                "gpu_info": GPU_INFO,
                "model": MODEL_FILE,
                "model_state": _current_state,
            }
            await ws.send(json.dumps(init_msg))
            logger.info("Sent init message to coordinator (protocol v%d, state=%s)", PROTOCOL_VERSION, _current_state)

            # ── Step 2: Start background tasks ───────────────────────────────
            heartbeat_task = asyncio.create_task(send_heartbeats(ws))
            gpu_task = asyncio.create_task(gpu_monitor_task(ws))
            idle_task = asyncio.create_task(idle_timeout_task(ws))

            logger.info("✅ Connected and awaiting inference jobs...")

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

                    elif msg_type == "server_notice":
                        level = data.get("level", "info")
                        message = data.get("message", "")
                        if level == "warning":
                            logger.warning("📢 Coordinator says: %s", message)
                        elif level == "error":
                            logger.error("📢 Coordinator says: %s", message)
                        else:
                            logger.info("📢 Coordinator says: %s", message)

                    else:
                        logger.debug("Ignoring message type: %s", msg_type)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket closed by coordinator")
            except websockets.exceptions.WebSocketException as e:
                logger.warning("WebSocket connection failed: %s — running in offline mode", e)
                # Continue running in offline mode, just don't send state updates
            except Exception as e:
                logger.warning("Unexpected WebSocket error: %s — running in offline mode", e)
                # Continue running in offline mode, just don't send state updates
            finally:
                heartbeat_task.cancel()
                gpu_task.cancel()
                idle_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass
                try:
                    await gpu_task
                except asyncio.CancelledError:
                    pass
                try:
                    await idle_task
                except asyncio.CancelledError:
                    pass
    except websockets.exceptions.WebSocketException as e:
        logger.warning("WebSocket connection failed: %s — running in offline mode", e)
        # Continue running in offline mode, just don't send state updates
    except Exception as e:
        logger.warning("Unexpected WebSocket error: %s — running in offline mode", e)
        # Continue running in offline mode, just don't send state updates
        # Continue running in offline mode, just don't send state updates
    except Exception as e:
        logger.warning("Unexpected WebSocket error: %s — running in offline mode", e)
        # Continue running in offline mode, just don't send state updates
    finally:
        if ws:
            heartbeat_task.cancel()
            gpu_task.cancel()
            idle_task.cancel()
            try:
                await heartbeat_task
            except asyncio.CancelledError:
                pass
            try:
                await gpu_task
            except asyncio.CancelledError:
                pass
            try:
                await idle_task
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
    If llama-server isn't running, start it first (load on demand).
    """
    global _active_requests, _last_request_time

    # Ensure model is loaded before processing
    async with _state_lock:
        _active_requests += 1
        _last_request_time = time.time()
        if not await _is_llama_running():
            # Load the model synchronously — the request will wait
            loaded = await _load_model(ws)
            if not loaded:
                _active_requests = max(0, _active_requests - 1)
                await ws.send(json.dumps({
                    "type": "inference_result",
                    "id": req_id,
                    "status": "error",
                    "error": "Failed to load model",
                }))
                return
        # Update state to busy now that we have active requests
        await _update_model_state(ws)

    logger.info("[%s] Forwarding request to llama-server...", req_id)

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
        logger.info("[%s] ✅ Review complete — result sent back to coordinator", req_id)

    except httpx.TimeoutException:
        logger.error("[%s] Request timed out against llama-server", req_id)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": "llama-server timed out",
        }))
    except httpx.RequestError as e:
        logger.error("[%s] Request failed against llama-server: %s", req_id, e)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": str(e),
        }))
    except Exception as e:
        logger.error("[%s] Unexpected error: %s", req_id, e)
        await ws.send(json.dumps({
            "type": "inference_result",
            "id": req_id,
            "status": "error",
            "error": str(e),
        }))
    finally:
        async with _state_lock:
            _active_requests = max(0, _active_requests - 1)
            _last_request_time = time.time()
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
