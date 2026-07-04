# AG-UI bridge: FastAPI/uvicorn serving POST /agui over the codex app-server.
# Build context is ./client (see docker-compose.slack.yml).
FROM python:3.11-slim

WORKDIR /app

# The package dir includes the reference web client (nanocodex_client/agui/web).
COPY pyproject.toml ./
COPY nanocodex_client ./nanocodex_client
RUN pip install --no-cache-dir .

# The ws capability token is discovered by the client's walk-up search for
# `secrets/ws-token`; the compose overlay mounts the shared secret at
# /app/secrets/ws-token. NANOCODEX_WS_TOKEN env overrides if set.
EXPOSE 8130
HEALTHCHECK --interval=10s --timeout=3s --retries=12 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8130/healthz', timeout=2)"]

CMD ["uvicorn", "nanocodex_client.agui.app:app", "--host", "0.0.0.0", "--port", "8130"]
