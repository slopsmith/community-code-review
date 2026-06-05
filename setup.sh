#!/bin/bash
# ──────────────────────────────────────────────────────────────────────────
#  Setup Script — Community Code Review
#  Run this once on the coordinator machine to get everything going.
# ──────────────────────────────────────────────────────────────────────────
set -e

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║     Community Code Review — Setup                            ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ── Step 1: Check prerequisites ──────────────────────────────────────────
echo "🔍 Checking prerequisites..."

if ! command -v git &>/dev/null; then
    echo "✗ Git not found. Install from https://git-scm.com/downloads"
    echo "  (On Windows, this also provides Git Bash for running this script.)"
    exit 1
fi
echo "  ✓ Git"

if ! command -v docker &>/dev/null; then
    echo "✗ Docker not found. Install from https://docs.docker.com/get-docker/"
    exit 1
fi
echo "  ✓ Docker"

if ! command -v tailscale &>/dev/null; then
    echo "✗ Tailscale not found. Install from https://tailscale.com/download"
    exit 1
fi

# Check Tailscale is logged in and connected
TAILSCALE_STATUS=$(tailscale status --json 2>/dev/null || echo '{"Self":null,"Status":"NoState"}')
# Check for "Online" being true anywhere in the output (handles any JSON spacing)
if ! echo "$TAILSCALE_STATUS" | grep -q '"Online"[[:space:]]*:'; then
    echo "  ⚠ Tailscale is installed but may not be logged in or connected."
    echo "  Please run: tailscale up"
    echo "  Then re-run this script."
    exit 1
fi
echo "  ✓ Tailscale (connected)"
echo ""

# ── Check if already configured ──────────────────────────────────────────
if [ -f .env ]; then
    echo "📂 Existing .env found — loading previous configuration..."
    set -a
    . .env
    set +a
    echo "  ✓ Loaded configuration"
    echo ""
    echo "🔁 Re-running setup — existing configuration preserved."
    echo "  To start fresh, remove the .env file and re-run."
    echo ""
fi

# ── Step 2: Generate volunteer secret (only if not already set) ──────────
if [ -z "$COORDINATOR_SECRET" ]; then
    echo "🔐 Generating volunteer secret..."
    COORDINATOR_SECRET=$(openssl rand -base64 32)
    echo "  ✓ Secret generated"
    echo ""
fi

# ── Step 3: Gather inputs (only if not already configured) ────────────────
if [ -z "$GITHUB_ORG_NAME" ]; then
    echo "Please enter your GitHub organization name:"
    read -r GITHUB_ORG_NAME
    echo ""
    echo "Now you need a GitHub Personal Access Token with admin:org scope."
    echo "  Create one at:"
    echo "    https://github.com/settings/tokens/new?description=self-hosted-runner&scopes=admin:org"
    echo ""
    echo "  Click \"Generate token\" and paste it below:"
    read -rs GITHUB_PAT
    echo ""
fi

# ── Step 4: Create .env file (only if missing) ───────────────────────────
if [ ! -f .env ]; then
    echo "📝 Creating .env file..."
    cat > .env << EOF
COORDINATOR_SECRET=${COORDINATOR_SECRET}
GITHUB_ORG_NAME=${GITHUB_ORG_NAME}
GITHUB_PAT=${GITHUB_PAT}
RUNNER_NAME=coordinator-runner
EOF
    echo "  ✓ .env created"
else
    echo "📝 .env file already exists — skipping (use teardown.sh to reset)"
fi
echo ""

# ── Step 5: Pull latest images and start/update containers ───────────────
RUNNING=$(docker compose ps --status running 2>/dev/null | grep -c "Up" || true)
if [ "$RUNNING" -ge 2 ]; then
    echo "🐳 Containers are running — checking for updates..."
else
    echo "🐳 Pulling latest images..."
fi
docker compose pull
echo "  ✓ Images up to date"
echo ""
echo "🐳 Starting coordinator and runner..."
docker compose up -d
echo "  ✓ Containers started"
echo ""
if [ "$RUNNING" -lt 2 ]; then
    echo "⏳ Waiting for runner to register with GitHub..."
    sleep 5
    echo "    https://github.com/organizations/${GITHUB_ORG_NAME}/settings/actions/runners"
    echo "  for 'coordinator-runner' (status: Idle)"
fi
echo ""

# ── Step 6: Tailscale Funnel setup (skip if already active) ──────────────
FUNNEL_ACTIVE=$(tailscale funnel status 2>/dev/null | grep -c "8080" || true)
if [ "$FUNNEL_ACTIVE" -gt 0 ]; then
    echo "🌐 Tailscale Funnel already active on port 8080 — skipping"
    FUNNEL_URL=$(tailscale funnel status 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || true)
else
    echo "🌐 Setting up Tailscale Funnel..."
    echo ""
    echo "  First, enable MagicDNS and HTTPS Certificates in your Tailscale admin console:"
    echo "    1. Go to https://login.tailscale.com/admin/dns"
    echo "    2. Enable MagicDNS (if not already on)"
    echo "    3. Enable HTTPS Certificates (if not already on)"
    echo ""
    echo "  Press Enter once you've done both..."
    read -r
    echo ""

    # Run funnel — --yes skips interactive prompts, --bg runs in background
    echo "→ Enabling Funnel on port 8080..."
    tailscale funnel --yes --bg 8080 2>&1 || sudo tailscale funnel --yes --bg 8080 2>&1 || true
    echo ""

    # Get the URL after funnel is authorized
    FUNNEL_URL=$(tailscale funnel status 2>&1 | grep -o 'https://[^ ]*' | head -1 || true)
    if [ -n "$FUNNEL_URL" ]; then
        echo "  ✓ Funnel is active at ${FUNNEL_URL}"
    else
        echo "  ⚠ Funnel may still be provisioning. Continuing..."
    fi
fi

if [ -z "$FUNNEL_URL" ]; then
    FUNNEL_URL="https://<your-machine>.<your-tailnet>.ts.net"
fi

# ── Step 7: Optional smoke test ──────────────────────────────────────────
echo ""
echo "🧪 Run a quick smoke test?"
echo "  This verifies the volunteer can connect to the coordinator."
echo "  You'll need a GPU for the full test (or it will fall back to CPU)."
echo ""
echo "  Options:"
echo "    [q] Quick test  — skips model load, tests connection only (~10s)"
echo "    [f] Full test   — downloads model and tests full pipeline (~20+ min)"
echo "    [N] Skip        — no smoke test"
echo ""
echo "  Run which test? [q/f/N]"
read -r SMOKE_TEST

MOCK_MODE=""
if [ "$SMOKE_TEST" = "q" ] || [ "$SMOKE_TEST" = "Q" ]; then
    MOCK_MODE="1"
    SMOKE_TEST="y"
elif [ "$SMOKE_TEST" = "f" ] || [ "$SMOKE_TEST" = "F" ]; then
    SMOKE_TEST="y"
else
    SMOKE_TEST="n"
fi

if [ "$SMOKE_TEST" = "y" ] || [ "$SMOKE_TEST" = "Y" ]; then
    echo ""
    echo "🧪 Building volunteer image..."
    cd "$(dirname "$0")/volunteer"
    docker build --no-cache -t volunteer:latest .
    cd - > /dev/null

    echo "🧪 Starting test volunteer..."
    # Remove any previous container with this name
    docker rm -f smoke-test-volunteer 2>/dev/null || true
    MSYS_NO_PATHCONV=1 docker run -d \
        --name smoke-test-volunteer \
        --gpus all \
        --add-host host.docker.internal:host-gateway \
        -v ~/smoke-test-models:/models \
        -e COORDINATOR_URL="http://host.docker.internal:8080" \
        -e VOLUNTEER_ID="smoke-test" \
        -e VOLUNTEER_SECRET="${COORDINATOR_SECRET}" \
        -e MOCK_MODE="${MOCK_MODE}" \
        -e MODEL_REPO="Qwen/Qwen3-30B-A3B-GGUF" \
        -e MODEL_FILE="Qwen3-30B-A3B-Q4_K_M.gguf" \
        volunteer:latest

    echo "⏳ Waiting for volunteer to register..."
    echo "  You can follow progress with: docker logs -f smoke-test-volunteer"
    echo ""

    # Poll for registration, abort if container fails
    if [ -n "$MOCK_MODE" ]; then
        POLL_MAX=24   # 2 minutes for quick test
    else
        POLL_MAX=240  # 20 minutes for full test
    fi
    REGISTERED=false
    for i in $(seq 1 $POLL_MAX); do
        # Check container is still running
        CONTAINER_STATUS=$(docker inspect smoke-test-volunteer --format '{{.State.Status}}' 2>/dev/null || true)
        if [ "$CONTAINER_STATUS" != "running" ]; then
            echo "  ✗ Container stopped ($CONTAINER_STATUS) — check logs above for errors"
            break
        fi

        VOLUNTEERS=$(curl -sf http://localhost:8080/volunteers 2>/dev/null || echo "[]")
        if echo "$VOLUNTEERS" | grep -q '"smoke-test"'; then
            REGISTERED=true
            break
        fi
        sleep 5
    done

    if [ "$REGISTERED" = true ]; then
        if [ -n "$MOCK_MODE" ]; then
            echo "✅ Quick test passed! Coordinator sees the volunteer (mock mode)."
        else
            echo "✅ Smoke test passed! Coordinator sees the volunteer."
        fi
        echo "  You can leave it running while you test PRs, or stop it now."
        echo ""
        echo "  Keep it running? [y/N]"
        read -r KEEP_VOLUNTEER
        if [ "$KEEP_VOLUNTEER" != "y" ] && [ "$KEEP_VOLUNTEER" != "Y" ]; then
            docker stop smoke-test-volunteer > /dev/null 2>&1 || true
            docker rm smoke-test-volunteer > /dev/null 2>&1 || true
            echo "  Test volunteer stopped and removed."
        fi
    else
    if [ -n "$MOCK_MODE" ]; then
        echo "⚠ Quick test inconclusive — volunteer did not register within 2 minutes."
    else
        echo "⚠ Smoke test inconclusive — volunteer did not register within 20 minutes."
        echo "  The model download may still be in progress."
    fi
        echo "  Check logs: docker logs -f smoke-test-volunteer"
        echo ""
        echo "  To clean up the test container:"
        echo "    docker stop smoke-test-volunteer"
        echo "    docker rm smoke-test-volunteer"
    fi
    echo ""
fi

echo ""
echo "  ✅ Setup complete. The coordinator is ready!"
echo ""
echo "  Tell volunteers to run this command:"
echo ""
echo "    MSYS_NO_PATHCONV=1 docker run -d --gpus all \ "
echo "      --name code-review-volunteer \ "
echo "      -v ~/code-review-models:/models \ "
echo "      -e COORDINATOR_URL=\"${FUNNEL_URL}\" \ "
echo "      -e VOLUNTEER_SECRET=\"${COORDINATOR_SECRET}\ " \"
echo "      -e VOLUNTEER_ID=\"github-or-discord-name\" \ "
echo "      volunteer:latest"
echo ""
echo "  Need to stop everything later? Run:"
echo ""
echo "    ./teardown.sh"
echo ""
