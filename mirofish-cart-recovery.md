# MiroFish Cart Recovery Skill

Use this skill when the user wants to integrate the MiroFish prediction engine into a Shopify cart recovery tool, or wants to understand/extend the cart recovery flow.

## What MiroFish Does

MiroFish is a multi-agent AI prediction engine. It:
1. Takes **seed documents** (text describing a real-world scenario) + a **prediction requirement**
2. Builds a **knowledge graph** (Zep Cloud) of entities from those documents
3. Generates **LLM-powered agent personas** for each entity (personality, MBTI, sentiment, stance)
4. Runs a **social media simulation** (OASIS: Twitter/Reddit) where agents interact
5. Uses a **ReportAgent** (ReACT pattern) to analyse emergent behaviour into a structured prediction report

## Cart Recovery Integration (V1)

**Flow:**
```
Shopify webhook (cart abandoned)
        ↓
Format customer + cart data → plain-text seed document
        ↓
MiroFish: simulate shopper psychology (24h simulation, Twitter)
        ↓
ReportAgent: predicted abandonment reason, emotional state, key objections
        ↓
EmailPromptBuilder: build LLM prompt context
        ↓
Claude / GPT-4: generate personalised recovery email
        ↓
Send via Klaviyo / SendGrid / Postmark
```

## Prerequisites

1. **MiroFish backend running** (one of):
   ```bash
   # Docker
   docker build -f backend/Dockerfile -t mirofish-backend . && docker run -p 5001:5001 --env-file backend/.env mirofish-backend

   # Local
   cd backend && python run.py
   ```

2. **Environment variables** (in `backend/.env`):
   ```
   LLM_API_KEY=your_api_key
   LLM_BASE_URL=https://api.openai.com/v1          # or Aliyun/other OpenAI-compat
   LLM_MODEL_NAME=gpt-4o-mini
   ZEP_API_KEY=your_zep_cloud_key
   ```

3. **Python client installed**:
   ```bash
   pip install MiroFish-main/client
   ```

## Python Integration (CartRecoveryEngine — recommended)

The `cart_recovery` module provides a high-level engine. Drop it into your Python backend:

```python
import sys
sys.path.insert(0, "path/to/MiroFish-main")

from cart_recovery import CartRecoveryEngine, ShopifyCartData

engine = CartRecoveryEngine(
    mirofish_url="http://localhost:5001",
    enable_reddit=False,    # Twitter-only for V1 speed
    simulation_hours=24,    # 24 simulated hours is enough for psychology insight
)

cart = ShopifyCartData(
    customer_id="cust_7821",
    customer_name="Sarah Mitchell",
    email="sarah@example.com",
    cart_items=[
        {"product": "Wireless Headphones", "price": 149.99, "quantity": 1},
    ],
    cart_total=149.99,
    exit_page="checkout/payment",
    abandoned_at_step="payment",
    past_orders=2,
    device="mobile",
    location="Austin, TX, USA",
    referral_source="instagram",
)

insight = engine.analyze_abandonment(cart)

# insight fields:
# - predicted_reason:   str  (why they left)
# - emotional_state:    str  (price-sensitive | anxious | indecisive | ...)
# - recommended_angle:  str  (discount-or-value | trust-and-social-proof | urgency-scarcity | ...)
# - key_objections:     list[str]
# - email_prompt_context: str  ← paste into Claude/GPT to generate the email

print(insight.email_prompt_context)
```

**Generate the email** (Anthropic example):
```python
import anthropic

client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": insight.email_prompt_context}],
)
recovery_email = message.content[0].text
```

## Python Integration (Low-level MiroFishClient)

Use this for custom pipeline control:

```python
from mirofish import MiroFishClient

client = MiroFishClient("http://localhost:5001")

# Step 1: Upload seed doc + build graph
project = client.generate_ontology(files=["cart_seed.txt"], requirement="...")
project = client.build_graph(project.project_id)

# Step 2: Create + prepare simulation
sim = client.create_simulation(project.project_id, requirement, enable_twitter=True, enable_reddit=False)
sim = client.prepare_simulation(sim.simulation_id)

# Step 3: Run simulation
client.start_simulation(sim.simulation_id)
run_status = client.wait_for_simulation(sim.simulation_id, timeout=3600)

# Step 4: Report
report = client.generate_report(sim.simulation_id)
print(report.content)

# Step 5: Interview agents
answer = client.interview_agent(sim.simulation_id, "What was the customer's biggest hesitation?")
```

## TypeScript / Next.js Integration

```typescript
// Install: no package needed — uses native fetch
const MIROFISH = "http://localhost:5001";

async function runCartRecovery(seedText: string, requirement: string) {
  // 1. Upload + ontology
  const form = new FormData();
  form.append("files", new Blob([seedText]), "cart.txt");
  form.append("requirement", requirement);
  const ontologyRes = await fetch(`${MIROFISH}/api/graph/ontology/generate`, { method: "POST", body: form });
  const { project } = await ontologyRes.json();

  // 2. Build graph (async poll)
  const { task_id } = await (await fetch(`${MIROFISH}/api/graph/build/${project.project_id}`, { method: "POST" })).json();
  await pollUntil(`/api/graph/status/${task_id}`, (t) => ["COMPLETED", "FAILED"].includes(t.task.status), 3000);

  // 3. Create simulation
  const { simulation } = await (await fetch(`${MIROFISH}/api/simulation/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: project.project_id, simulation_requirement: requirement, enable_twitter: true }),
  })).json();

  // 4. Prepare
  await fetch(`${MIROFISH}/api/simulation/${simulation.simulation_id}/prepare`, { method: "POST" });
  await pollUntil(`/api/simulation/${simulation.simulation_id}/prepare_status`, (s) => s.status === "READY", 10000);

  // 5. Run
  await fetch(`${MIROFISH}/api/simulation/${simulation.simulation_id}/start`, { method: "POST" });
  await pollUntil(`/api/simulation/${simulation.simulation_id}/run_status`, (s) => ["completed","failed"].includes(s.runner_status), 30000);

  // 6. Report
  await fetch(`${MIROFISH}/api/report/${simulation.simulation_id}/generate`, { method: "POST" });
  await pollUntil(`/api/report/${simulation.simulation_id}/status`, (s) => s.status === "completed", 5000);
  const { content } = await (await fetch(`${MIROFISH}/api/report/${simulation.simulation_id}/full`)).json();
  return content;
}

async function pollUntil(path: string, isDone: (data: any) => boolean, intervalMs: number): Promise<void> {
  while (true) {
    const data = await (await fetch(MIROFISH + path)).json();
    if (isDone(data)) return;
    await new Promise(r => setTimeout(r, intervalMs));
  }
}
```

## Go Integration

```go
// See client/examples/example_go_client.go for the full implementation.
// Key pattern: HTTP POST with multipart for file upload, JSON for other calls.
// Poll /api/graph/status/{task_id} (3s interval) and /api/simulation/{id}/run_status (30s).

// Quick usage:
// 1. Build seed doc string from your cart struct
// 2. POST multipart to /api/graph/ontology/generate
// 3. POST to /api/graph/build/{project_id} → poll task
// 4. POST to /api/simulation/create → POST /prepare → poll → POST /start → poll
// 5. POST to /api/report/{sim_id}/generate → poll → GET /full
```

## Seed Document Format

MiroFish works best when the seed document includes:

```
Customer Profile: [Name]

[Name] is a [new/returning] customer from [location].
They accessed the store via [device] from [referral source].

Abandoned Cart Contents:

  - [Product] × [qty] @ $[price] [category]
Cart total: $[total]

Browsing Behavior:

Total time on site: [N] minutes.
Pages visited: [page1, page2, ...]
Last page before leaving: [exit_page].
Checkout step reached before abandonment: [step].

Purchase History:

[First visit / X previous orders, lifetime spend $Y]

Abandonment Context:

[Why they likely left based on the step they abandoned]

Social Context:

Similar Shopper A is price-conscious...
Similar Shopper B responds to urgency...
Brand Advocate is a loyal customer...
```

The `ShopifyFormatter` class in `cart_recovery/shopify_formatter.py` generates this automatically from a `ShopifyCartData` object.

## API Reference

| Endpoint | Method | Description |
|---|---|---|
| `/api/graph/ontology/generate` | POST (multipart) | Upload files + requirement → project |
| `/api/graph/build/{project_id}` | POST | Start graph build → task_id |
| `/api/graph/status/{task_id}` | GET | Poll graph build progress |
| `/api/simulation/create` | POST | Create simulation → simulation_id |
| `/api/simulation/{id}/prepare` | POST | Start profile/config generation |
| `/api/simulation/{id}/prepare_status` | GET | Poll preparation progress |
| `/api/simulation/{id}/start` | POST | Launch OASIS processes |
| `/api/simulation/{id}/run_status` | GET | Poll execution state |
| `/api/simulation/{id}/run_status_detail` | GET | + recent_actions array |
| `/api/simulation/{id}/stop` | POST | Stop running simulation |
| `/api/simulation/{id}/actions` | GET | All logged agent actions |
| `/api/simulation/{id}/timeline` | GET | Round-by-round summaries |
| `/api/report/{id}/generate` | POST | Start report generation |
| `/api/report/{id}/status` | GET | Poll report generation |
| `/api/report/{id}/full` | GET | Get full markdown report |
| `/api/report/{id}/interview` | POST | Interview ReportAgent |

## Tuning for Cart Recovery

| Parameter | V1 value | Notes |
|---|---|---|
| `enable_twitter` | `true` | Captures social dynamics |
| `enable_reddit` | `false` | Skip for V1 speed |
| `simulation_hours` | `24` | Enough for psychology insight |
| Seed doc length | 500–2000 chars | More detail = richer personas |
| Agent count | ~5–8 | Customer + peer archetypes |

## V2 Roadmap Hooks

- **Predict recovery likelihood**: After simulation, call `/api/simulation/{id}/actions` and count engagement with the customer-persona agent. High engagement = higher conversion probability.
- **Full autonomous agent**: Use `client.interview_agent(sim_id, "What discount would bring this customer back?")` to drive dynamic offer generation.
- **Batch processing**: Run `analyze_abandonment()` in a worker queue (Celery, BullMQ, Go goroutines) for concurrent cart events.

## Files in This Integration

```
MiroFish-main/
├── client/
│   ├── mirofish/          # Python SDK (pip install ./client)
│   └── examples/
│       ├── cart_recovery_example.py   # Python demo
│       ├── example_go_client.go       # Go reference
│       └── example_ts_client.ts       # TypeScript reference
└── cart_recovery/
    ├── engine.py           # CartRecoveryEngine (main entry point)
    ├── shopify_formatter.py  # ShopifyCartData → seed document
    └── email_prompt_builder.py  # Report → LLM email prompt
```
