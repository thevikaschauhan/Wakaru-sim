# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## What This Is

**MiroFish** is a multi-agent AI prediction engine that creates digital simulations populated by LLM-powered autonomous agents. It predicts real-world outcomes by simulating social dynamics rather than using statistical models.

In the context of the **Vakaru cart recovery system**, MiroFish acts as the primary abandonment reasoning engine: it takes Shopify cart + customer data, simulates the shopper's psychology, and returns structured insight used to write personalised recovery emails.

---

## Commands

```bash
# Start the Flask backend (port 5001 by default)
cd backend && python run.py

# Docker (build + run the backend image)
docker build -f backend/Dockerfile -t mirofish-backend . && docker run -p 5001:5001 --env-file backend/.env mirofish-backend

# Install Python deps
pip install -r backend/requirements.txt
```

**Required env vars** (root `.env` — loaded by `backend/app/config.py`):
```
LLM_API_KEY         OpenAI or compatible API key
LLM_BASE_URL        https://api.openai.com/v1  (or Aliyun/other OpenAI-compat)
LLM_MODEL_NAME      gpt-4o-mini  (or qwen-plus)
ZEP_API_KEY         Zep Cloud API key (for knowledge graphs)
FLASK_PORT          5001  (default)
OASIS_DEFAULT_MAX_ROUNDS   10  (default simulation rounds)
```

**Port conventions when running alongside Vakaru:**
- MiroFish: `5001` (standalone) or `5002` (when running alongside Vakaru's GRU server on 5001)
- Vakaru Engine: `8080`

---

## Architecture

### The 5-Step Pipeline

```
1. Seed Documents (PDF/MD/TXT)
        ↓  services/ontology_generator.py
   Entity ontology (exactly 10 entity types)
        ↓  services/graph_builder.py + Zep Cloud
   Knowledge graph (entities, relationships, facts)

2. Graph Entities
        ↓  services/zep_entity_reader.py
        ↓  services/oasis_profile_generator.py
   Agent personas (MBTI, profession, sentiment bias, influence weight, posting schedule)

3. SimulationParameters
        ↓  services/simulation_config_generator.py  (4 LLM calls)
        ↓  services/simulation_manager.py
   Twitter + Reddit OASIS simulation configs

4. OASIS Simulation
        ↓  services/simulation_runner.py
   Parallel Twitter + Reddit processes; each round = configurable simulated minutes
   Every agent action logged to actions.jsonl

5. Post-simulation
        ↓  services/report_agent.py  (ReACT pattern, max 5 tool calls × 2 rounds)
   Structured markdown prediction report
```

### API Blueprints (all prefixed `/api/`)

| Blueprint | Prefix | Key Endpoints |
|---|---|---|
| `cart_recovery_bp` | `/cart-recovery` | `POST /analyze` (sync), `POST /jobs` + `GET /jobs/{id}` (async, #20) ← **Vakaru integration point** |

`cart_recovery_bp` is the only blueprint; the OASIS `graph_bp`/`simulation_bp`/`report_bp` endpoints were removed (#62) once the pipeline ran in-process (#19). The pipeline stages are invoked directly as Python services (see Services Directory), not over HTTP.

### Services Directory

```
backend/app/services/
├── ontology_generator.py       LLM → entity type definitions from text
├── graph_builder.py            Zep Cloud graph construction + chunking
├── zep_entity_reader.py        Reads + filters entities from Zep graph
├── oasis_profile_generator.py  LLM → OASIS agent personas (parallel, 3 at a time)
├── simulation_config_generator.py  LLM → behavioral parameters (4 sequential calls)
├── simulation_manager.py       Preparation orchestration + state machine
├── simulation_runner.py        OASIS process management + action logging
├── report_agent.py             ReACT post-simulation analysis
└── zep_tools.py                InsightForge + PanoramaSearch tools for ReportAgent
```

---

## Cart Recovery Integration (Vakaru)

### The Entry Point

`POST /api/cart-recovery/analyze` (`backend/app/api/cart_recovery.py`) is the
Vakaru integration point. Since issue #19 the handler runs the pipeline
**in-process** via `backend/app/services/cart_recovery_workflow.py` →
`run_cart_recovery(cart)`, which calls the backend services directly
(ontology → graph → simulation → report). It does **not** self-HTTP back into
this Flask process.

The in-process pipeline delegates to two helpers under `cart_recovery/`:
`ShopifyFormatter` (`shopify_formatter.py`) formats the cart into seed text, and
`EmailPromptBuilder` (`email_prompt_builder.py`) turns the report into the
`AbandonmentInsight`. (The former external SDK — `cart_recovery/engine.py` and the
`client/` package — was removed; the pipeline is in-process only.)

### `ShopifyCartData` — Input Schema

All fields that improve analysis accuracy (expand as more Shopify data becomes available):
```python
customer_id, customer_name, email, checkout_token
cart_items: [{product, variant, sku, price, quantity, vendor, category}]
cart_total, cart_subtotal, cart_tax, currency
discount_codes: [], discount_amount, discount_type  # CRITICAL signal
shipping_cost, shipping_method, shipping_country    # CRITICAL signal
payment_gateway_attempted, payment_method_type      # CRITICAL signal
browsing_history, collections_viewed, products_viewed, products_removed
searches_submitted, alert_messages_shown
time_on_site_minutes, exit_page, abandoned_at_step
device_type, viewport_width, language, market, referral_source, utm_campaign, utm_source
past_orders, total_spend_lifetime, is_first_order   # from Admin API
customer_tags, email_marketing_consent
hours_since_last_abandonment                        # serial abandoner signal
```

### `AbandonmentInsight` — Output Schema

```python
predicted_reason: str          # "Shipping cost shock at checkout"
reason_category: str           # shipping_cost | price_sensitivity | sizing_doubt | payment_friction | just_browsing | out_of_stock_concern | unknown  (issue #3)
emotional_state: str           # price-sensitive | anxious | indecisive | comparison-shopping | trust-lacking | distracted
recommended_angle: str         # discount-or-value | trust-and-social-proof | urgency-scarcity | welcome-and-reassurance | loyalty-and-reward | gentle-reminder
key_objections: list[str]      # ["$18.99 shipping on $45 order", "No free shipping threshold shown"]
email_prompt_context: str      # Full LLM prompt — paste into GPT-4/Claude to generate email
confidence: float              # 0.0-1.0 heuristic confidence in the analysis
confidence_reasoning: str      # one-sentence explanation of the confidence score
```

### Vakaru → MiroFish API Contract

The `cart_recovery_bp` in `backend/app/api/cart_recovery.py` exposes:

```
POST /api/cart-recovery/analyze
Content-Type: application/json

{
  "customer_id": "cust_123",
  "customer_name": "Sarah Mitchell",
  "email": "sarah@example.com",
  "cart_items": [{"product": "...", "price": 99.99, "quantity": 1, "sku": "..."}],
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
  "browsing_history": ["Homepage", "Headphones", "Cart", "Checkout"],
  ...
}

Response (200):
{
  "success": true,
  "data": {
    "predicted_reason": "...",
    "reason_category": "shipping_cost",
    "emotional_state": "...",
    "recommended_angle": "...",
    "key_objections": [...],
    "email_prompt_context": "...",
    "confidence": 0.62,
    "confidence_reasoning": "..."
  }
}

# Async alternative (issue #20) — enqueue instead of blocking 8-17 min:
POST /api/cart-recovery/jobs           # same request body as above
→ 202 { "success": true, "job_id": "<id>", "status_url": "/api/cart-recovery/jobs/<id>" }

GET /api/cart-recovery/jobs/<job_id>
→ 200 { "success": true, "status": "queued|started|finished|failed",
        "progress": {...}, "result": {<data block, same shape as /analyze>}?, "error": "Analysis failed (<Type>)"? }
```

---

## Simulation Timing

For cart recovery simulations (24 simulated hours, Twitter-only, ~5 agents):

| Stage | Real wall-clock time |
|---|---|
| Ontology generation | 15–30s |
| Graph build (Zep) | 30–120s |
| Agent profile generation | 2–5 min (LLM per agent, parallel batch of 3) |
| Config generation | 30–60s (4 LLM calls) |
| OASIS simulation (24 sim hours ÷ 60 min/round = 24 rounds) | 4–8 min |
| Report generation | 1–3 min (ReACT, 2–3 LLM calls) |
| **Total** | **~8–17 min** |

Set `enable_reddit=False` and `simulation_hours=24` for the fastest cart recovery analysis. Use `enable_reddit=True` and `simulation_hours=72` for deeper multi-email sequence analysis.

**LLM cost estimate (gpt-4o-mini):** ~$0.03–0.07 per cart event.

---

## Simulation State Machine

```
CREATED → PREPARING → READY → RUNNING → COMPLETED
                                      → STOPPED
                                      → FAILED
```

These states are tracked in-process by `SimulationManager`/`SimulationRunner` and advanced synchronously by `run_cart_recovery` — there is no HTTP polling (the old `/api/simulation/*` status endpoints were removed with the OASIS prune, #62). `runner_status` ∈ {running, completed, stopped, failed}; progress surfaces via the `on_progress` callback and the `/api/cart-recovery/jobs/<id>` progress block.

---

## Frontend (`web/` inside engine-main)

There is a small Next.js app inside `engine-main/web/` for monitoring simulation runs (displays actions, status). Not part of the Vakaru merchant dashboard — it's a MiroFish-specific debugging UI.

---

## Key Design Decisions

- **Zep Cloud** is a hard dependency for knowledge graphs. No fallback if Zep is down — analysis fails at graph build stage.
- **Agent profiles are generated in parallel** (3 at a time, configurable). Scale this up for larger simulations.
- **Simulation is stateless** — each cart-recovery run (the in-process `run_cart_recovery()`) creates an isolated project + simulation. No shared memory between cart events. This invariant still holds after issue #19 (the in-process path mints a fresh project + simulation per call).
- **The GRU classifier** (`engine-main/classification/`) is a separate POC system. MiroFish does not use it. Do not confuse the two.
- **Simulation seed document quality** directly drives output quality. The more rich, contextual data in the seed doc (shipping cost, payment method, browsing history, customer history), the better the personas and the more accurate the predicted abandonment reason.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **wakaru-main** (1501 symbols, 6776 relationships, 129 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/wakaru-main/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/wakaru-main/context` | Codebase overview, check index freshness |
| `gitnexus://repo/wakaru-main/clusters` | All functional areas |
| `gitnexus://repo/wakaru-main/processes` | All execution flows |
| `gitnexus://repo/wakaru-main/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
