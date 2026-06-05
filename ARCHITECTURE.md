# Community Code Review — Architecture

## Overview

The system has three components:

1. **Coordinator** (Python FastAPI on the leader's machine) — Exposes a single OpenAI-compatible API endpoint, load-balances across volunteers via WebSocket tunnels.
2. **Volunteer Agent** (Python + `llama-server` on community machines) — Auto-downloads the model, starts `llama-server`, opens a **persistent outbound WebSocket** connection to the coordinator, and waits for work.
3. **GitHub Actions Workflow** (per-repo) — Triggers `ocr review` on PRs, running on the organization's self-hosted runner.

### Design Principles

- **Zero-friction for volunteers** — set three env vars, run one command, you're done
- **Gamer-friendly** — volunteers automatically step aside when their GPU is busy; no manual toggles needed
- **Defense in depth** — volunteers make only outbound connections; GPU and model volume are the only host resources shared
- **Truly no inbound** — the coordinator never connects to volunteers; volunteers connect outbound and stay connected
- **Sensible defaults, optional overrides** — everything has a default; only customization is opt-in

## Network Topology

```
                   ┌─────────────────────┐
                   │    GitHub Actions    │
                   │  (public internet)   │
                   └─────────┬───────────┘
                             │ Dispatch job to self-hosted runner
                   ┌─────────▼───────────┐
                   │ Self-Hosted Runner   │
                   │ (leader's machine)   │
                   └─────────┬───────────┘
                             │ POST /v1/chat/completions
                   ┌─────────▼───────────┐
                   │    Coordinator       │
                   │  (leader's machine)  │
                   │  :8080 (HTTP + WS)   │
                   └─────────┬───────────┘
                             │ WebSocket (outbound from volunteers)
                             │ Requests pushed through tunnel
            ┌────────────────┼────────────────┐
            │                │                │
     ┌──────▼──────┐  ┌──────▼──────┐  ┌──────▼──────┐
     │Volunteer 1  │  │Volunteer 2  │  │Volunteer N  │
     │ WebSocket → │  │ WebSocket → │  │ WebSocket → │
     │ llama-srv   │  │ llama-srv   │  │ llama-srv   │
     └─────────────┘  └─────────────┘  └─────────────┘
            ▲                ▲                ▲
            │   Behind NAT / Firewall (home networks)
            └────────────────┼────────────────┘
```

The critical insight: **volunteers open the WebSocket outbound** and keep it open.
The coordinator never initiates a TCP connection to the volunteer — it sends work
through the already-established WebSocket tunnel.

## Volunteer Lifecycle & GPU Awareness

Volunteers move through four states, tracked by the coordinator:

| State | Description | Accepts work? |
|-------|-------------|---------------|
| `unloaded` | Connected, no model in VRAM | ✅ Yes (will load on demand) |
| `loading` | Currently loading model into VRAM | ❌ No |
| `ready` | Model loaded, GPU idle | ✅ Yes (preferred) |
| `busy` | Model loaded, actively inferencing | ✅ Yes, if parallel slots available |

### GPU Utilization Monitoring (Automatic)

The volunteer agent polls GPU utilization every 5 seconds and reports it to the coordinator:

```json
{"type": "gpu_status", "utilization_percent": 12, "memory_used_percent": 45}
```

If the volunteer's GPU is busy with other work (gaming, rendering, etc.), the volunteer **unloads the model from VRAM** and notifies the coordinator. The coordinator stops sending new assignments, and the volunteer's GPU is completely free for other tasks. When the GPU quiets down, the volunteer returns to the `unloaded` state — connected and ready to reload on the next request.

**Thresholds** (configurable via environment variables on the coordinator):
- `GPU_UTIL_THRESHOLD` — default 70%
- `GPU_MEM_THRESHOLD` — default 85%

A volunteer exceeding either threshold transitions to `unloaded` (if not actively inferencing) or finishes the current request and then unloads.

### Scheduling Priority

The coordinator assigns work in this order:

1. **`ready` volunteers** with lowest `active_requests / max_parallel` ratio
2. **`busy` volunteers** with remaining capacity (`active_requests < max_parallel`)
3. **`unloaded` volunteers** (they'll load on demand — adds 30–60s latency)
4. **Never pick `loading`** — they'd just queue behind their own load

This minimizes time-to-review while respecting volunteer GPU availability.

## Connections

| Direction | Protocol | Purpose |
|-----------|----------|---------|
| Volunteer → Coordinator | **WebSocket** (outbound) | Persistent tunnel: registration (`init`), heartbeat, **receiving work**, GPU status |
| Self-hosted runner → Coordinator | HTTP (outbound) | `ocr review` API calls |
| Volunteer → Hugging Face / URL | HTTPS (outbound) | Model download (if not cached) |

Every single connection is **initiated outbound** by the volunteer or the runner.
No inbound firewall holes needed anywhere — works behind NAT, CGNAT, or strict corporate firewalls.

## API Contracts

### Coordinator ↔ Volunteer (WebSocket tunnel)

Volunteers connect to `ws://<coordinator>:8080/ws/<volunteer_id>` and keep the
connection open. The coordinator sends work requests as JSON messages over this
WebSocket, and the volunteer sends responses back over the same connection.

**Message from coordinator → volunteer (inference request):**
```json
{
  "type": "inference",
  "id": "req-uuid",
  "body": { "model": "...", "messages": [...], "max_tokens": 4096, "temperature": 0.7, "stream": false }
}
```

**Message from volunteer → coordinator (inference response):**
```json
{
  "type": "inference_result",
  "id": "req-uuid",
  "status": "success",
  "body": { "id": "...", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "..."}, "finish_reason": "stop"}] }
}
```

On error:
```json
{ "type": "inference_result", "id": "req-uuid", "status": "error", "error": "message" }
```

**Message from volunteer → coordinator (GPU status):**
```json
{"type": "gpu_status", "utilization_percent": 12, "memory_used_percent": 45}
```

**Message from volunteer → coordinator (model state):**
```json
{"type": "model_state", "state": "loading"}
{"type": "model_state", "state": "ready", "load_duration_seconds": 42.5, "max_parallel": 4}
{"type": "model_state", "state": "unloading"}
{"type": "model_state", "state": "unloaded"}
```

The volunteer also sends periodic heartbeat pings over the WebSocket.

### OCR → Coordinator (OpenAI-compatible)

Full OpenAI Chat Completions API: `POST /v1/chat/completions`

```
Request:  {model, messages, max_tokens, temperature, stream: false}
Response: {id, object, created, model, choices: [{index, message: {role, content}, finish_reason}]}
Auth:     Authorization: Bearer <token> (optional, configurable)
```

### Volunteer → Coordinator (WebSocket registration)

| Mechanism | Timing | Payload |
|-----------|--------|---------|
| **WebSocket `init` message** | On connect (first message) | `{"type": "init", "gpu_info": "...", "model": "..."}` |
| **WebSocket `heartbeat` message** | Every 30s | `{"type": "heartbeat"}` |
| **WebSocket `gpu_status` message** | Every 5s | `{"type": "gpu_status", "utilization_percent": 12, "memory_used_percent": 45}` |
| **WebSocket `model_state` message** | On state change | `{"type": "model_state", "state": "ready", "load_duration_seconds": 42.5}` |

All happen over the **same persistent WebSocket** connection — no separate HTTP endpoints needed.

## Key Design Decisions

### Why llama.cpp instead of LM Studio?

LM Studio has no official Docker image and `lm-link` is designed for peer-to-peer setups with no single API endpoint. `llama.cpp` provides official `ghcr.io/ggml-org/llama.cpp:server-cuda` Docker images, exposes a correct OpenAI-compatible API, and is MIT-licensed.

### Why 32K context by default?

Code reviews need to see entire files. The Qwen3-30B-A3B model comfortably handles 32K context, and `llama-server` with prompt caching makes repeated context windows efficient. Users can lower to 8K on constrained hardware via the `LLAMA_CTX_SIZE` env var.

### Why auto-download models?

Volunteers shouldn't need to hunt for model files. The entrypoint downloads from Hugging Face with clear progress output so users always know what's happening.

### Why GPU_DEVICES=0 as default?

Most volunteers have a single GPU. Auto-detect the first one. `GPU_DEVICES=none` falls back to CPU. `GPU_DEVICES=all` uses every available GPU. `GPU_DEVICES=0,1` selects specific devices.

### Why automatic GPU utilization monitoring?

Volunteers are often gamers. We don't want to grab their GPU while they're using it, and we don't want them to have to think about toggling anything. The container monitors its own GPU and quietly steps aside when busy.

## Security Model

| Concern | Mitigation |
|---------|------------|
| Volunteer host access | Only `--gpus` and model volume mount; no `--privileged`, no host network |
| Network access | Only outbound WebSocket + HTTP to coordinator URL |
| Volunteer behind NAT | Works natively — volunteers initiate and maintain the WebSocket connection |
| Authentication | Coordinator validates volunteer identity during WebSocket handshake via `VOLUNTEER_SECRET` |
| PR data leakage | All traffic over HTTPS/TLS recommended for production coordinator |
| Rogue volunteer joining | Coordinator validates `VOLUNTEER_SECRET` on WebSocket connect |