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

The entire pipeline runs **in-process** behind a single blueprint, `cart_recovery_bp`.
Callers POST the cart payload as JSON; MiroFish runs ontology → graph → simulation →
report internally (`backend/app/services/cart_recovery_workflow.py` →
`run_cart_recovery`) and returns the structured insight. There is **no** external
Python/Go/TS SDK and **no** granular `/api/graph`, `/api/simulation`, or `/api/report`
HTTP surface — those were removed once the pipeline moved in-process (#19/#62).

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

## Integration — the cart-recovery API

Send the cart as JSON; MiroFish returns the `AbandonmentInsight` data block.

**Sync** (blocks ~8–17 min — use only for testing / one-off runs):
```
POST /api/cart-recovery/analyze
Content-Type: application/json

{ "customer_id": "cust_123", "cart_items": [...], "cart_total": 149.99,
  "currency": "USD", "shipping_cost": 12.99, "exit_page": "checkout/payment", ... }

→ 200 { "success": true, "data": {
    "predicted_reason": "...", "reason_category": "shipping_cost",
    "emotional_state": "...", "recommended_angle": "...",
    "key_objections": [...], "email_prompt_context": "...",
    "confidence": 0.62, "confidence_reasoning": "..." } }
```

**Async** (production path — enqueue, then poll; issue #20):
```
POST /api/cart-recovery/jobs           # same request body as /analyze
→ 202 { "success": true, "job_id": "<id>", "status_url": "/api/cart-recovery/jobs/<id>" }

GET /api/cart-recovery/jobs/<job_id>
→ 200 { "success": true, "status": "queued|started|finished|failed",
        "progress": {...}, "result": {<same data block as /analyze>}?, "error": "..."? }
```

`email_prompt_context` is a ready-to-use LLM prompt — paste it into Claude/GPT to
generate the recovery email:
```python
import anthropic

client = anthropic.Anthropic()
message = client.messages.create(
    model="claude-opus-4-6",
    max_tokens=512,
    messages=[{"role": "user", "content": insight["email_prompt_context"]}],
)
recovery_email = message.content[0].text
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

The `ShopifyFormatter` class in `cart_recovery/shopify_formatter.py` generates this
automatically from a `ShopifyCartData` object; `EmailPromptBuilder`
(`cart_recovery/email_prompt_builder.py`) turns the report into the `AbandonmentInsight`.

## Tuning for Cart Recovery

| Parameter | V1 value | Notes |
|---|---|---|
| Twitter | on | Captures social dynamics |
| Reddit | off | Skip for V1 speed |
| Simulation hours | `24` | Enough for psychology insight |
| Seed doc length | 500–2000 chars | More detail = richer personas |
| Agent count | ~5–8 | Customer + peer archetypes |

## Files in This Integration

```
cart_recovery/
├── shopify_formatter.py     # ShopifyCartData → seed document text
├── email_prompt_builder.py  # report → AbandonmentInsight + LLM email prompt
└── recovery_spec.py         # shared cart-recovery constants / spec

backend/app/services/cart_recovery_workflow.py   # run_cart_recovery() — in-process pipeline
backend/app/api/cart_recovery.py                 # the /api/cart-recovery blueprint
```
