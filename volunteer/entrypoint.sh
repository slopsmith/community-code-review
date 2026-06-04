#!/bin/bash
set -e
COORDINATOR_URL="${COORDINATOR_URL:?COORDINATOR_URL is required}"
VOLUNTEER_ID="${VOLUNTEER_ID:-$(hostname)-$$}"
MODEL_REPO="${MODEL_REPO:-Qwen/Qwen3-30B-A3B-GGUF}"
MODEL_FILE="${MODEL_FILE:-Qwen3-30B-A3B-Q4_K_M.gguf}"
MODEL_URL="${MODEL_URL:-}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_CTX_SIZE="${LLAMA_CTX_SIZE:-32768}"
LLAMA_N_PARALLEL="${LLAMA_N_PARALLEL:-1}"
LLAMA_TEMP="${LLAMA_TEMP:-0.7}"
MODEL_PATH="/models/${MODEL_FILE}"
LLAMA_N_GPU_LAYERS="${LLAMA_N_GPU_LAYERS:-99}"

check_vram() {
    local info=$(curl -sf "https://huggingface.co/api/models/${MODEL_REPO}" 2>/dev/null) || return 0
    local size=$(echo "$info" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for s in d.get('siblings',[]):
    if '${MODEL_FILE}' in s.get('rfilename',''):
        print(s.get('size',0))
        sys.exit(0)
print(0)
" 2>/dev/null) || size=0
    [ "$size" -le 0 ] && return 0
    local model_mb=$(( size / 1024 / 1024 ))
    local vram_total=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader 2>/dev/null | head -1 | grep -oE '[0-9]+') || vram_total=0
    echo "  Model: ~${model_mb}MiB, VRAM: ${vram_total}MiB total"
    if [ "$vram_total" -gt 0 ] && [ "$model_mb" -gt "$vram_total" ]; then
        # Scale layers proportionally — model layers / VRAM ratio
        local scaled=$(( LLAMA_N_GPU_LAYERS * vram_total / model_mb ))
        [ "$scaled" -lt 1 ] && scaled=1   # always use GPU for at least 1 layer
        LLAMA_N_GPU_LAYERS=$scaled
        echo "  Partial offload: ${LLAMA_N_GPU_LAYERS} GPU layers (auto-scaled)"
    fi
}

while true; do
    echo "=== Container start ==="
    GPU_INFO="CPU (no GPU detected)"
    if command -v nvidia-smi &>/dev/null; then
        smi=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -1)
        [ -n "$smi" ] && GPU_INFO="$smi"
    fi
    case "${GPU_DEVICES:-}" in
        "")  [ "$GPU_INFO" != "CPU" ] && export CUDA_VISIBLE_DEVICES="0" || LLAMA_N_GPU_LAYERS=0 ;;
        "none") LLAMA_N_GPU_LAYERS=0 ;;
        "all") echo "$GPU_INFO" | grep -qi cpu && LLAMA_N_GPU_LAYERS=0 || unset CUDA_VISIBLE_DEVICES ;;
        *)   export CUDA_VISIBLE_DEVICES="$GPU_DEVICES" ;;
    esac
    export GPU_INFO
    echo "  GPU: $GPU_INFO, Layers: $LLAMA_N_GPU_LAYERS"
    check_vram || true

    if [ ! -f "$MODEL_PATH" ]; then
        dl="${MODEL_URL:-https://huggingface.co/${MODEL_REPO}/resolve/main/${MODEL_FILE}?download=true}"
        echo "  Downloading model..."
        curl -# -L "$dl" -o "${MODEL_PATH}.tmp" 2>&1
        mv "${MODEL_PATH}.tmp" "$MODEL_PATH"
        echo "  Download complete"
    else
        echo "  Model found: $MODEL_PATH"
    fi

    echo "  Starting llama-server..."
    /app/llama-server -m "$MODEL_PATH" --host 0.0.0.0 --port "$LLAMA_PORT" -ngl "$LLAMA_N_GPU_LAYERS" -c "$LLAMA_CTX_SIZE" -np "$LLAMA_N_PARALLEL" --temp "$LLAMA_TEMP" --no-ui --no-warmup &
    LLAMA_PID=$!
    ok=0
    for i in $(seq 1 30); do
        if curl -sf "http://localhost:${LLAMA_PORT}/health" >/dev/null 2>&1; then ok=1; break; fi
        sleep 1
    done
    if [ "$ok" -eq 0 ]; then echo "  Server failed, restarting..."; kill "$LLAMA_PID" 2>/dev/null || true; sleep 5; continue; fi

    echo "  Starting agent..."
    cd /app && python3 agent.py &
    AGENT_PID=$!
    trap "kill $LLAMA_PID $AGENT_PID 2>/dev/null; exit 0" SIGTERM SIGINT
    echo "  Volunteer running"
    wait -n 2>/dev/null || wait
    echo "=== Process ended, restarting in 5s ==="
    sleep 5
done
