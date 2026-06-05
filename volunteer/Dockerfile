# ── Select GPU backend at build time ────────────────────────────────────
# Usage: docker build --build-arg BACKEND=cuda -t volunteer:latest .
# Supported backends:
#   cuda    → NVIDIA GPUs (default)
#   rocm    → AMD GPUs
#   vulkan  → Any GPU with Vulkan drivers (NVIDIA, AMD, Intel, Apple)
#   intel   → Intel GPUs (SYCL)
#   cpu     → CPU only (no GPU acceleration)
ARG BACKEND=cuda
FROM ghcr.io/ggml-org/llama.cpp:server-${BACKEND}

ARG ORG_NAME
LABEL org.opencontainers.image.title="Community Code Review Volunteer"
LABEL org.opencontainers.image.description="Run this container to donate GPU compute for automated AI code reviews on GitHub pull requests."
LABEL org.opencontainers.image.documentation="https://github.com/${ORG_NAME}/community-code-review/blob/main/docs/VOLUNTEER_SETUP.md"
LABEL org.opencontainers.image.source="https://github.com/${ORG_NAME}/community-code-review"
LABEL org.opencontainers.image.licenses="MIT"

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-pip python3-venv curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /venv
ENV PATH="/venv/bin:$PATH"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py .
COPY MODEL_README.md .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/models"]
EXPOSE 8080

# The only host path volunteers must share — model storage
VOLUME ["/models"]

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]