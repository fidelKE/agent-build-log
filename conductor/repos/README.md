# conductor/repos/

External repositories cloned here for local Docker builds.
These are not committed to the Conductor repo — only the setup instructions are.

Add to .gitignore:
  conductor/repos/mem0/

## mem0 (Sprint 4)

The Mem0 server is built from source. There is no pre-built Docker image.

### Prerequisites

A `conductor/.env` must exist with `ANTHROPIC_API_KEY` and `LLM_GATEWAY_URL`.
If coming from Sprint 3:

```bash
cp conductor/sprint-04-secrets-storage/.env conductor/sprint-05-memory-evals/.env
# Add Sprint 4 vars:
cat >> conductor/sprint-05-memory-evals/.env << 'EOF'
MEMORY_PROVIDER=inmemory
REDIS_HOST=localhost
REDIS_PORT=6379
QDRANT_HOST=localhost
QDRANT_PORT=6333
MEM0_SERVER_URL=http://localhost:8888
MEM0_API_KEY=
MEM0_POSTGRES_PASSWORD=mem0-dev
MEM0_JWT_SECRET=conductor-dev-jwt-secret
EOF
# Symlink so the canonical compose can read it
ln -sf sprint-05-memory-evals/.env conductor/.env
```

### Step 1 — Clone

```bash
cd conductor/repos
git clone https://github.com/mem0ai/mem0
```

### Step 2 — Build and start

```bash
cd conductor
podman compose --profile mem0-server up -d --build
```

First run takes ~3-5 min (builds server + dashboard images, pulls pgvector).

### Step 3 — Wait for the server

```bash
until curl -fsS http://localhost:8888/auth/setup-status >/dev/null 2>&1; do
  echo "waiting..."; sleep 3
done
echo "ready"
```

### Step 4 — Bootstrap admin + API key (first run only)

```bash
API_URL=http://localhost:8888 \
EMAIL=admin@conductor.dev \
PASSWORD=conductor-dev \
NAME=Admin \
bash conductor/repos/mem0/server/scripts/seed.sh
```

Copy the printed API key and add it to `.env`:

```bash
echo "MEM0_API_KEY=<paste key here>" >> conductor/sprint-05-memory-evals/.env
```

### Step 5 — Verify

```bash
curl -s http://localhost:8888/auth/setup-status
# → {"needsSetup":false}

curl -s -X POST http://localhost:8888/memories \
  -H "X-API-Key: $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "Snowflake JDBC auth failure"}], "user_id": "test-1"}'

curl -s -X POST http://localhost:8888/search \
  -H "X-API-Key: $MEM0_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query": "Snowflake", "filters": {"user_id": "test-1"}, "top_k": 3}'
```

Dashboard: http://localhost:3001

### Known issues resolved during setup

- `python:3.12-slim` is missing `libpq` — our `mem0-server.Dockerfile` uses `python:3.12` (full)
- `claude-haiku-4-5` rejected by Bedrock (temperature+top_p conflict) — compose uses `gpt-4.1-mini`
- SQLite history db needs a writable path — mounted as `mem0-server-data` volume at `/app/data`
- Healthcheck uses `/auth/setup-status`, not `/healthz`

### Stop

```bash
cd conductor
podman compose --profile mem0-server down
```
