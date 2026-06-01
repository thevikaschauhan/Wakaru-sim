# Wakaru — Shopper Psychology Engine

> The AI abandonment reasoning engine powering [Vakaru](https://getvakaru.com) cart recovery.

Wakaru is a multi-agent simulation engine that predicts **why** a shopper abandoned their cart — not by running statistical models, but by simulating the shopper's psychology in a digital environment populated by LLM-powered autonomous agents.

When a Shopify cart is abandoned, Vakaru sends the cart data to Wakaru. Wakaru builds a knowledge graph from the cart context, spawns agents that mirror the shopper's likely persona, runs a social simulation, and returns structured abandonment insight — which Vakaru uses to write a personalised recovery email.

---

## How It Works

```
Shopify cart data (items, shipping, discounts, browsing history)
        ↓
  Seed document generation
        ↓
  Entity ontology (10 entity types extracted via LLM)
        ↓
  Knowledge graph (Zep Cloud — entities, relationships, facts)
        ↓
  Agent persona generation (MBTI, sentiment bias, influence weight)
        ↓
  OASIS simulation (24 simulated hours, Twitter environment)
        ↓
  ReportAgent analysis (ReACT pattern, tool-augmented reasoning)
        ↓
  AbandonmentInsight { predicted_reason, emotional_state,
                       recommended_angle, key_objections,
                       email_prompt_context }
```

The full pipeline runs in **8–17 minutes** wall-clock time and costs roughly **$0.03–0.07** per cart event (gpt-4o-mini).

---

## API

### Cart Recovery Endpoint

```
POST /api/cart-recovery/analyze
Content-Type: application/json
```

**Request:**

```json
{
  "customer_id": "cust_123",
  "customer_name": "Sarah Mitchell",
  "email": "sarah@example.com",
  "cart_items": [
    { "product": "Wireless Headphones", "price": 99.99, "quantity": 1, "sku": "WH-100" }
  ],
  "cart_total": 99.99,
  "currency": "USD",
  "shipping_cost": 12.99,
  "shipping_method": "Standard (5-7 days)",
  "discount_codes": [],
  "payment_gateway_attempted": "paypal",
  "exit_page": "checkout/payment",
  "abandoned_at_step": "payment",
  "past_orders": 2,
  "is_first_order": false,
  "browsing_history": ["Homepage", "Headphones", "Cart", "Checkout"]
}
```

**Response** (`200`):

```json
{
  "success": true,
  "data": {
    "predicted_reason": "Shipping cost shock at checkout — $12.99 on a $99.99 order",
    "emotional_state": "price-sensitive",
    "recommended_angle": "discount-or-value",
    "key_objections": [
      "$12.99 shipping on a $99 order feels disproportionate",
      "No free shipping threshold was shown earlier in the journey"
    ],
    "email_prompt_context": "Write a cart recovery email for Sarah who abandoned...",
    "confidence": 0.62,
    "confidence_reasoning": "Strong shipping-cost signal; limited browsing history."
  }
}
```

> ⚠️ `POST /analyze` is **synchronous** — it blocks for the full 8–17 min pipeline.
> New integrations should prefer the async job API below (issue #20).

### Async Cart Recovery (Redis + RQ)

`POST /api/cart-recovery/jobs` validates the payload (same body + same `400`s as
`/analyze`), enqueues the analysis on Redis, and returns immediately so the
caller never holds an 8–17 min connection:

```
POST /api/cart-recovery/jobs          # same body as /analyze
→ 202 { "success": true, "job_id": "<id>", "status_url": "/api/cart-recovery/jobs/<id>" }
```

Poll for progress and the terminal result:

```
GET /api/cart-recovery/jobs/<job_id>
→ 200 {
    "success": true,
    "job_id":  "<id>",
    "status":  "queued | started | finished | failed",
    "progress": { "stage": "...", "state": { ... } },          // PII-free, while running
    "result":  { ...the same 7-field `data` block as /analyze },  // when finished
    "error":   "Analysis failed (<ExceptionType>)"                // when failed (PII-free)
  }
```

Requires `REDIS_URL`; `/jobs` returns `503` if the queue is unavailable (`/analyze`
is unaffected). The RQ worker runs as a separate Railway service and scales
independently of the web workers.

**Operational notes**

- **Failed jobs are terminal** — no automatic retry is configured. An 8–17 min,
  paid LLM run must not silently re-run, and there is no idempotency yet (issue
  #12). On `status: "failed"` the caller decides whether to re-`POST /jobs`.
- **Deploying the worker** — a *separate* Railway service off the same image with
  start command `python worker.py` (from `/app/backend`). Set `REDIS_URL` on
  **both** the web and worker services. **Disable the worker service's health
  check**: the shared `railway.toml` sets `healthcheckPath = /health`, but the
  worker serves no HTTP and would otherwise be marked unhealthy and restart-looped.
- **Throughput** — concurrency scales by worker *replicas*, not gunicorn
  `--workers`. The enqueue p95 latency and 20-concurrent behaviour are exercised
  in unit tests only against in-memory `fakeredis`; run a live load test against
  real Redis before production scale-up.

### Other Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/graph/ontology/generate` | Generate entity ontology from seed doc |
| `POST` | `/api/graph/graph/build/{id}` | Build Zep knowledge graph |
| `GET`  | `/api/graph/status/{task_id}` | Poll graph build status |
| `POST` | `/api/simulation/create` | Create simulation |
| `POST` | `/api/simulation/{id}/prepare` | Prepare agents + config |
| `POST` | `/api/simulation/{id}/start` | Start OASIS simulation |
| `GET`  | `/api/simulation/{id}/run_status` | Poll simulation progress |
| `POST` | `/api/report/{id}/generate` | Generate prediction report |
| `GET`  | `/api/report/{id}/full` | Retrieve full report |

---

## Quick Start

### Prerequisites

| Tool | Version | Check |
|------|---------|-------|
| Python | ≥ 3.11, ≤ 3.12 | `python --version` |
| Node.js | ≥ 18 | `node -v` |

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:

```env
# LLM (OpenAI-compatible)
LLM_API_KEY=sk-...
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL_NAME=gpt-4o-mini

# Zep Cloud (knowledge graphs)
ZEP_API_KEY=z_...

# Optional tuning
FLASK_PORT=5001
OASIS_DEFAULT_MAX_ROUNDS=10
```

> **Zep Cloud** is a hard dependency. Get a free API key at [app.getzep.com](https://app.getzep.com).

### 2. Install and run

```bash
# Install all dependencies (Node + Python)
npm run setup:all

# Start the backend (port 5001)
cd backend && python run.py
```

### 3. Run the cart recovery example

```bash
pip install ./client
python client/examples/cart_recovery_example.py
```

### Docker (recommended for production)

```bash
cp .env.example .env
# Fill in API keys in .env
docker compose up -d
```

---

## Python Client SDK

```python
from cart_recovery import CartRecoveryEngine, ShopifyCartData

engine = CartRecoveryEngine("http://localhost:5001")

cart = ShopifyCartData(
    customer_id="cust_123",
    customer_name="Sarah Mitchell",
    email="sarah@example.com",
    cart_items=[{"product": "Headphones", "price": 99.99, "quantity": 1}],
    cart_total=99.99,
    shipping_cost=12.99,
    exit_page="checkout/payment",
    abandoned_at_step="payment",
)

insight = engine.analyze_abandonment(cart)
print(insight.predicted_reason)       # "Shipping cost shock at checkout"
print(insight.emotional_state)        # "price-sensitive"
print(insight.email_prompt_context)   # Paste into GPT-4/Claude to generate email
```

---

## Simulation Timing

For a typical cart recovery analysis (24 simulated hours, ~5 agents):

| Stage | Wall-clock time |
|-------|----------------|
| Ontology generation | 15–30s |
| Graph build (Zep) | 30–120s |
| Agent profile generation | 2–5 min |
| Config generation | 30–60s |
| OASIS simulation (24 rounds) | 4–8 min |
| Report generation | 1–3 min |
| **Total** | **~8–17 min** |

**LLM cost:** ~$0.03–0.07 per cart event (gpt-4o-mini).

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_API_KEY` | ✅ | — | OpenAI-compatible API key |
| `LLM_BASE_URL` | ✅ | `https://api.openai.com/v1` | LLM API base URL |
| `LLM_MODEL_NAME` | ✅ | `gpt-4o-mini` | Model name |
| `ZEP_API_KEY` | ✅ | — | Zep Cloud API key |
| `FLASK_PORT` | — | `5001` | Backend port |
| `FLASK_DEBUG` | — | `False` | Debug mode (never `True` in production) |
| `OASIS_DEFAULT_MAX_ROUNDS` | — | `10` | Simulation rounds |
| `SECRET_KEY` | — | auto-generated | Flask session secret |

---

## Architecture

```
backend/app/services/
├── ontology_generator.py        LLM → entity type definitions from cart text
├── graph_builder.py             Zep Cloud graph construction
├── zep_entity_reader.py         Reads entities from Zep graph
├── oasis_profile_generator.py   LLM → OASIS agent personas (parallel, 3 at a time)
├── simulation_config_generator.py  LLM → behavioral parameters (4 sequential calls)
├── simulation_manager.py        Preparation orchestration + state machine
├── simulation_runner.py         OASIS process management + action logging
├── report_agent.py              ReACT post-simulation analysis
└── zep_tools.py                 InsightForge + PanoramaSearch tools

cart_recovery/
├── engine.py                    CartRecoveryEngine — main Vakaru entry point
├── shopify_formatter.py         ShopifyCartData → seed document text
└── email_prompt_builder.py      Report → AbandonmentInsight + LLM prompt
```

**Simulation state machine:**
```
CREATED → PREPARING → READY → RUNNING → COMPLETED
                                       → STOPPED
                                       → FAILED
```

---

## Powered By

- **[OASIS](https://github.com/camel-ai/oasis)** — open-source social simulation framework by CAMEL-AI
- **[Zep Cloud](https://www.getzep.com)** — knowledge graph memory for agents
- **[Gunicorn](https://gunicorn.org)** — production WSGI server

---

## Deployment

Wakaru is deployed as a standalone service alongside the Vakaru engine.

**Production (Railway):** `https://mirofish-production-e968.up.railway.app`

**Port conventions:**
- Wakaru standalone: `5001`
- Wakaru alongside Vakaru engine: `5002`
- Vakaru engine: `8080`
