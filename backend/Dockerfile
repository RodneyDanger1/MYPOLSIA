FROM python:3.12-slim

# Install system deps + Node.js (for claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CHROMA_PERSIST_DIR=/app/data/chroma \
    CLAUDE_CLI_PATH=/usr/local/bin/claude

EXPOSE 8000

# F1 (H10): liveness probe for the API role of this image.  GET /api/v1/health is the no-auth
# endpoint (backend/app/api/v1/health.py) on the port uvicorn serves (docker-compose.yml's backend
# `command`); curl is installed above, so no extra dependency.  NOTE: this image also backs the
# celery worker/beat services, which serve no HTTP — docker-compose.yml disables this inherited
# check for them (`healthcheck: disable: true`); keep the two files in sync if the probe changes.
HEALTHCHECK --interval=10s --timeout=5s --retries=12 --start-period=30s \
  CMD curl -fsS http://localhost:8000/api/v1/health || exit 1
