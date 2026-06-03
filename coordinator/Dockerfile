FROM python:3.14-alpine

LABEL org.opencontainers.image.title="Community Code Review Coordinator"
LABEL org.opencontainers.image.description="Central relay server for community-powered AI code reviews. Receives requests from GitHub Actions, distributes inference to volunteer containers."
LABEL org.opencontainers.image.source="https://github.com/slopsmith/community-code-review"
LABEL org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]