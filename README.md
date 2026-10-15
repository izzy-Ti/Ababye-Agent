# Merigeta Agent: Private Personal Telegram AI Assistant

Merigeta Agent is a secure, single-tenant, private AI personal assistant built for Telegram. It couples an edge gateway (Cloudflare Worker / Node.js) with a containerized FastAPI backend, local Model Context Protocol (MCP) tool subprocesses, and Google Gemini (`gemini-2.5-flash`).

---

## 1. System Architecture

```
[Telegram Cloud]
       │  (HTTPS POST Webhook with X-Telegram-Bot-Api-Secret-Token)
       ▼
[Edge Worker (Cloudflare / Node)]
   ├─ Constant-time Webhook Secret Verification
   ├─ Strict Sender ID & Private Chat Filter (Drops unauthorized senders silently)
   ├─ Media/Caption/Voice Extraction & Input Normalization
   ├─ Deduplication (Update ID & In-flight locking)
   ├─ Async typing heartbeat (action: "typing")
   └─ Dispatches Request to Backend (Internal Bearer Token)
       │
       ▼
[Backend (FastAPI Service)]
   ├─ Internal Bearer Token Authentication
   ├─ Allowed User Enforcement (`ALLOWED_TELEGRAM_USER_ID`)
   ├─ Deduplication & Persistent Request Cache (SQLite)
   ├─ Voice Note Transcription (Whisper API / Gemini Multimodal)
   ├─ Memory & Context Management (`chat_memory` table)
   ├─ Bounded Tool Loop (Max turns, step timeouts, tool call budgets)
   ├─ Two-Phase Write Action Confirmation Flow (`/confirm <TOKEN>`, `/cancel`)
   ├─ Isolated MCP Subprocess Manager (No core secrets leaked to tools)
   ├─ Google Gemini Provider (`gemini-2.5-flash`)
   ├─ Background Queue & Worker (Job state machine: pending -> running -> completed)
   └─ Signed Callback Delivery (HMAC-SHA256 with timestamp)
       │
       ▼
[Edge Worker Callback Receiver]
   ├─ Validates HMAC-SHA256 Signature (`X-Callback-Signature`)
   ├─ Verifies Timestamp Freshness (< 300s)
   └─ Delivers Unicode-safe Chunks to Telegram (`sendMessage`)
```

---

## 2. Directory Structure

```
├── .env.example                # Canonical environment variable reference
├── Dockerfile                  # Non-root container with pinned dependencies
├── mcp_config.json             # MCP servers configuration (e.g. echo, filesystem)
├── backend/
│   ├── app/
│   │   ├── agent.py            # Bounded agent loop, per-chat locks, write action confirmation
│   │   ├── audio.py            # Voice note intake, size limits, transcription, media cleanup
│   │   ├── gemini_provider.py  # Google Gemini 2.5 Flash model adapter & tool calling
│   │   ├── main.py             # FastAPI entrypoint, /ready, /healthz, /assistant, /jobs
│   │   ├── mcp_manager.py      # MCP subprocess lifecycle, session timeouts, secret filtering
│   │   ├── queue.py            # Durable job queue, SQLite worker, signed callback dispatcher
│   │   ├── storage.py          # SQLite storage, migrations, deduplication, retention, usage limits
│   │   └── structured_logging.py # Redacting JSON logger (scrubs tokens, URLs, PII)
│   └── tests/                  # Pytest test suite (74 tests including acceptance suite)
├── edge/
│   ├── src/
│   │   ├── index.ts            # Hono / Cloudflare Worker edge webhook handler & callback receiver
│   │   ├── telegram.ts         # Telegram API client, safe chunking, typing heartbeat
│   │   └── register_webhook.ts # Automated webhook registration CLI
│   └── tests/                  # Node.js test suite (44 unit and integration tests)
└── docs/
    └── IGNORE_POLICY.md        # Edge drop/ignore specification for unauthorized traffic
```

---

## 3. Prerequisites & Installation

### Requirements
- **Python**: 3.11+
- **Node.js**: 20+ (supports experimental strip-types)
- **Docker**: For containerized deployment

### Setup
1. Clone the repository:
   ```bash
   git clone https://github.com/izzy-Ti/Merigeta-Agent.git
   cd Merigeta-Agent
   ```
2. Configure environment:
   ```bash
   cp .env.example .env
   ```
   Fill in your `TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `ALLOWED_TELEGRAM_USER_ID`, `INTERNAL_SERVICE_TOKEN`, and `GEMINI_API_KEY`.
3. Install Backend Dependencies:
   ```bash
   cd backend
   pip install -r requirements.lock
   cd ..
   ```
4. Install Edge Dependencies:
   ```bash
   cd edge
   npm install
   cd ..
   ```

---

## 4. Running the Tests

### Edge Test Suite (44 Tests)
```bash
npm --prefix edge test
```
Tests cover:
- Constant-time webhook secret authentication
- Strict sender ID and private chat type evaluation
- Unsupported media, forwards, and caption handling
- Typing controller heartbeat and cleanup
- Unicode-safe message chunking and 429 rate limit backoff
- HMAC-SHA256 signed callback validation and replay rejection
- Webhook registration script logic

### Backend Test Suite (74 Tests)
```bash
python -m pytest backend/tests/ -v
```
Tests cover:
- MCP subprocess lifecycle, session timeouts, and tool allowlisting
- Model adapter and tool execution
- Bounded turn limits, tool budgets, and per-chat lock serialization
- Path traversal, SSRF, command injection, and env leakage defenses
- Durable request deduplication across service restarts
- Voice intake, transcription, and size/format enforcement
- Two-phase write confirmation flow and repeated execution blocks
- Queue worker persistence, crash survival, and signed callback delivery
- Structured log redaction of tokens and media URLs
- Daily usage caps, retention pruning, and failure alerts

### Complete Acceptance Suite (Matrix Tests A through J)
```bash
python -m pytest backend/tests/test_acceptance_suite.py -v
```
Verifies all 10 core security, isolation, and robustness properties specified in `guide.pdf`.

---

## 5. Deployment

### A. Docker Container (Backend)
The backend runs as a non-root user (`appuser`, UID 10001) with persistent data mounted to `/app/data`:

```bash
# Build the container
docker build -t merigeta-backend:latest .

# Run the container
docker run -d \
  --name merigeta-backend \
  --restart unless-stopped \
  -p 8000:8000 \
  -v $(pwd)/data:/app/data \
  --env-file .env \
  merigeta-backend:latest
```

### B. Health and Readiness Checks
- **Healthcheck Endpoint**: `GET http://localhost:8000/healthz` (returns `{"status": "healthy"}`)
- **Readiness Endpoint**: `GET http://localhost:8000/readyz` (verifies SQLite database connectivity, storage write capability, and MCP readiness)

### C. Edge Webhook Registration
To register the edge worker URL with Telegram's Webhook API:
```bash
node edge/dist/register_webhook.js \
  --url https://your-edge-domain.workers.dev/webhook \
  --secret YOUR_TELEGRAM_WEBHOOK_SECRET \
  --token YOUR_TELEGRAM_BOT_TOKEN
```

Verify webhook status at any time:
```bash
curl -s "https://api.telegram.org/bot<YOUR_TELEGRAM_BOT_TOKEN>/getWebhookInfo"
```

---

## 6. Operational Runbooks

### Runbook 1: Rotating Telegram Bot Token and Edge Secrets
1. Generate a new webhook secret:
   ```bash
   openssl rand -hex 32
   ```
2. Obtain a new Bot Token from `@BotFather` if rotating the bot token.
3. Update edge environment variables in Cloudflare Worker dashboard or `.env`:
   - `TELEGRAM_BOT_TOKEN`
   - `TELEGRAM_WEBHOOK_SECRET`
4. Register the new webhook with Telegram:
   ```bash
   node edge/dist/register_webhook.js \
     --url https://your-edge-domain.workers.dev/webhook \
     --secret <NEW_WEBHOOK_SECRET> \
     --token <NEW_BOT_TOKEN>
   ```
5. Confirm with `curl https://api.telegram.org/bot<NEW_BOT_TOKEN>/getWebhookInfo`.

### Runbook 2: Restarting Stalled / Dead MCP Servers
If an MCP tool crashes or becomes unresponsive:
1. The backend automatically catches subprocess termination and cleans up dead sessions.
2. To trigger a forced reset of all MCP servers without restarting the FastAPI server:
   - Send `POST /assistant` with `/reset` command from your authorized chat.
   - Or restart the backend container:
     ```bash
     docker restart merigeta-backend
     ```
3. Inspect structured logs for exit codes and error outputs:
   ```bash
   docker logs merigeta-backend --tail 100 | grep "mcp"
   ```

### Runbook 3: Draining the Job Queue Safely
When performing maintenance or container updates:
1. Pause accepting new incoming jobs by placing the edge worker into maintenance mode or setting `MAX_CONCURRENCY=0`.
2. Inspect remaining pending and running jobs in SQLite:
   ```bash
   sqlite3 data/merigeta.db "SELECT id, status, chat_id, created_at FROM jobs WHERE status IN ('pending', 'running');"
   ```
3. Let active jobs finish. The backend worker gracefully finishes the current job before terminating when receiving `SIGTERM`.
4. Stale jobs (`running` for longer than 600s) are automatically resumed or marked failed by the worker recovery sweep.

### Runbook 4: Restoring the SQLite Database from Backup
Database data is stored at `/app/data/merigeta.db` (and `data/merigeta.db` on host).
1. Stop the backend container:
   ```bash
   docker stop merigeta-backend
   ```
2. Create a timestamped backup of current state:
   ```bash
   cp data/merigeta.db data/merigeta.db.bak.$(date +%s)
   ```
3. Restore from backup file:
   ```bash
   cp /path/to/backup/merigeta_backup.db data/merigeta.db
   ```
4. Verify database integrity:
   ```bash
   sqlite3 data/merigeta.db "PRAGMA integrity_check;"
   ```
5. Start backend container and check readiness:
   ```bash
   docker start merigeta-backend
   curl -f http://localhost:8000/readyz
   ```

### Runbook 5: Changing the Allowed User List Without Downtime
1. The authorized Telegram user ID is defined by `ALLOWED_TELEGRAM_USER_ID`.
2. Both edge and backend check this ID:
   - Edge silently ignores any sender whose ID does not match.
   - Backend returns `403 Forbidden` if an unapproved user ID arrives.
3. Update `ALLOWED_TELEGRAM_USER_ID` in your environment config (Cloudflare Worker secrets and Docker `.env`).
4. Re-deploy edge secrets (instantaneous) and restart/reload backend container:
   ```bash
   docker-compose up -d --no-deps backend
   ```
   No database migration or data loss occurs.

### Runbook 6: Daily Usage Caps & Retention Pruning
- **Daily Usage Limits**: Defined by `DAILY_REQUEST_CAP` (default: 200). Requests exceeding this limit receive a friendly quota exhaustion message without calling the LLM.
- **Retention Pruning**: Old completed requests and expired write actions are pruned automatically every 24 hours. To invoke an immediate prune:
  ```python
  from backend.app.storage import storage
  import asyncio
  asyncio.run(storage.cleanup_retention_data(retention_days=7))
  ```
- **Failure Alerting**: Consecutive unhandled provider failures trigger structured alerts at threshold (default: 3 failures). The counter resets upon the next successful user transaction.
