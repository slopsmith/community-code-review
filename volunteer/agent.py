"""
Volunteer WebSocket Agent — connects to coordinator for inference.

Connects to the coordinator via a persistent outbound WebSocket tunnel,
receives inference requests, sends them to the local llama-server via HTTP,
and returns results. This agent runs alongside llama-server in the container.

Every connection is outbound-initiated — works behind NAT, CGNAT, firewalls.

ZZ Woz 'ere (aka-testing functionality) 
"""

import asyncio
import json
import logging
import os
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

# Derive WebSocket URL from the coordinator URL
WS_BASE = COORDINATOR_URL.replace("http://", "ws://").replace("https://", "wss://")
WS_URL = f"{WS_BASE}/ws/{VOLUNTEER_ID}"

if VOLUNTEER_SECRET:
    WS_URL += f"?secret={urllib.parse.quote(VOLUNTEER_SECRET, safe='')}"

LLAMA_API_URL = f"http://localhost:{LLAMA_PORT}/v1/chat/completions"


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
            "gpu_info": GPU_INFO,
            "model": MODEL_FILE,
        }
        await ws.send(json.dumps(init_msg))
        logger.info("Sent init message to coordinator")

        # ── Step 2: Start heartbeat task ─────────────────────────────────
        heartbeat_task = asyncio.create_task(send_heartbeats(ws))

        try:
            # ── Step 4: Message loop ────────────────────────────────────
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
            try:
                await heartbeat_task
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


if __name__ == "__main__":
    logger.info("Starting volunteer WebSocket agent...")
    logger.info("  Volunteer ID:  %s", VOLUNTEER_ID)
    logger.info("  Coordinator:   %s", COORDINATOR_URL)
    logger.info("  GPU:           %s", GPU_INFO)
    logger.info("  Model:         %s", MODEL_FILE)

    try:
        asyncio.run(run_agent())
    except KeyboardInterrupt:
        logger.info("Shutting down")
