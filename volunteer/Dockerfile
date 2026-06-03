# ── Stage 1: llama-server binary ──────────────────────────────────────────
FROM ghcr.io/ggml-org/llama.cpp:server-cuda AS llama

FROM python:3.14-alpine

ARG ORG_NAME
LABEL org.opencontainers.image.title="Community Code Review Volunteer"
LABEL org.opencontainers.image.description="Run this container to donate GPU compute for automated AI code reviews on GitHub pull requests. See VOLUNTEER_SETUP.md in the repository for setup instructions."
LABEL org.opencontainers.image.documentation="https://github.com/${ORG_NAME}/community-code-review/blob/main/docs/VOLUNTEER_SETUP.md"
LABEL org.opencontainers.image.source="https://github.com/${ORG_NAME}/community-code-review"
LABEL org.opencontainers.image.licenses="MIT"

# Copy llama-server from the official image
COPY --from=llama /app/llama-server /usr/local/bin/llama-server

# Alpine uses apk instead of apt-get
RUN apk add --no-cache curl

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the WebSocket agent
COPY agent.py .
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# The only host path volunteers must share — model storage
VOLUME ["/models"]

EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]