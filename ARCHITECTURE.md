# Community Code Review — Architecture

## Overview

The system has three components:

1. **Coordinator** (Python FastAPI on the leader's machine) — Exposes a single OpenAI-compatible API endpoint, load-balances across volunteers via WebSocket tunnels.
2. **Volunteer Agent** (Python + `llama-server` on community machines) — Auto-downloads the model, starts `llama-server`, opens a **persistent outbound WebSocket** connection to the coordinator, and waits for work.
3. **GitHub Actions Workflow** (per-repo) — Triggers `ocr review` on PRs, running on the organization's self-hosted runner.

### Design Principles

- **Zero-friction for volunteers** — set three env vars, run one command, you're done
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

## Connections

| Direction | Protocol | Purpose |
|-----------|----------|---------|
| Volunteer → Coordinator | **WebSocket** (outbound) | Persistent tunnel: registration (`init`), heartbeat, **receiving work** |
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

Both happen over the **same persistent WebSocket** connection — no separate HTTP endpoints needed.

## Key Design Decisions

### Why llama.cpp instead of LM Studio?

LM Studio has no official Docker image and `lm-link` is designed for peer-to-peer setups with no single API endpoint. `llama.cpp` provides official `ghcr.io/ggml-org/llama.cpp:server-cuda` Docker images, exposes a correct OpenAI-compatible API, and is MIT-licensed.

### Why 32K context by default?

Code reviews need to see entire files. The Qwen3-30B-A3B model comfortably handles 32K context, and `llama-server` with prompt caching makes repeated context windows efficient. Users can lower to 8K on constrained hardware via the `LLAMA_CTX_SIZE` env var.

### Why auto-download models?

Volunteers shouldn't need to hunt for model files. The entrypoint downloads from Hugging Face with clear progress output so users always know what's happening.

### Why GPU_DEVICES=0 as default?

Most volunteers have a single GPU. Auto-detect the first one. `GPU_DEVICES=none` falls back to CPU. `GPU_DEVICES=all` uses every available GPU. `GPU_DEVICES=0,1` selects specific devices.

## Security Model

| Concern | Mitigation |
|---------|------------|
| Volunteer host access | Only `--gpus` and model volume mount; no `--privileged`, no host network |
| Network access | Only outbound WebSocket + HTTP to coordinator URL |
| Volunteer behind NAT | Works natively — volunteers initiate and maintain the WebSocket connection |
| Authentication | Coordinator validates volunteer identity during WebSocket handshake via `VOLUNTEER_SECRET` |
| PR data leakage | All traffic over HTTPS/TLS recommended for production coordinator |
| Rogue volunteer joining | Coordinator validates `VOLUNTEER_SECRET` on WebSocket connect |