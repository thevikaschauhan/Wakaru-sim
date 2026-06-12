# Vakaru → Wakaru integration: authentication contract

How the vakaru-engine (the only production caller) must authenticate requests
to Wakaru's cart-recovery API. Two independent layers:

| Layer | Header(s) | Proves | Issue |
|---|---|---|---|
| Shared API key | `X-API-Key` | caller holds the service key | #10 |
| HMAC body signature | `X-Wakaru-Signature`, `X-Wakaru-Timestamp` | this exact body, from a secret-holder, sent recently | #11 |

Both are required on the paid POSTs. `/health` needs neither.

## X-API-Key (issue #10)

Every request under `/api/*` must carry `X-API-Key: <WAKARU_API_KEY>`.
Missing/wrong key → `401 {"error": "unauthorized"}`. Checked before the HMAC,
so a key failure is reported as `unauthorized`, never as a signature error.

## HMAC body signing (issue #11)

Applies to **POST** requests on the cart-recovery blueprint:

- `POST /api/cart-recovery/jobs`
- `POST /api/cart-recovery/analyze`

The poll `GET /api/cart-recovery/jobs/<id>` has no body to bind and is **not**
signed (X-API-Key still required).

### How to sign

```
timestamp = current unix time in WHOLE SECONDS at request SEND time
signature = hex( HMAC-SHA256( key   = WAKARU_INTERNAL_SECRET,
                              data  = "<timestamp>" + "." + <raw request body bytes> ) )
```

Send:

```
X-Wakaru-Timestamp: <timestamp>
X-Wakaru-Signature: <signature>          # lowercase hex; uppercase accepted
```

Go reference (what the engine does in `services/mirofish_service.go`):

```go
ts := strconv.FormatInt(time.Now().Unix(), 10)
mac := hmac.New(sha256.New, []byte(secret))
mac.Write([]byte(ts + "."))
mac.Write(body) // the exact bytes sent as the request body
sig := hex.EncodeToString(mac.Sum(nil))
```

### Timestamp semantics — the one rule that matters

**The timestamp is the request SEND time, never the cart-event time.** Cart
recovery is inherently delayed — the engine detects abandonment minutes to
hours after the cart event. Wakaru rejects timestamps outside **±300 seconds
(5 minutes) of its own clock**, so an event-time timestamp would be rejected
on every legitimate request. (The #12 `Idempotency-Key`, by contrast, is
derived from the event/body and is deliberately stable across retries.)

A retry of the same request must be **re-signed with a fresh timestamp**; the
`Idempotency-Key` (unchanged across retries) is what dedups the paid work.

### Rejection responses

| Condition | Status | Body |
|---|---|---|
| header(s) absent | 401 | `{"error": "missing_signature"}` |
| timestamp not an integer | 401 | `{"error": "invalid_timestamp"}` |
| timestamp outside ±300 s | 401 | `{"error": "expired_timestamp"}` |
| signature does not verify | 401 | `{"error": "invalid_signature"}` |
| server has no secret configured | 503 | `{"error": "server_auth_not_configured"}` |

Order of checks on a POST: X-API-Key (#10) → rate limit (#12) → HMAC (#11) →
handler validation. Signature failures are therefore rate-charged like any
other request.

### Configuration

`WAKARU_INTERNAL_SECRET` must be set to the **same value** in three places:

1. Wakaru web service (verifies)
2. Wakaru worker service (boot-gate parity — `Config.validate()` runs on both)
3. vakaru-engine (signs)

Boot fails fast if the var is missing, whitespace, or the `.env.example`
placeholder. Generate with `openssl rand -hex 32`.

The engine signs **conditionally** (omits the headers when its secret is
unset), which makes the deploy order safe: engine first, then Wakaru
enforcement.

### Cross-repo contract test

Both suites pin the identical fixed vector so a unilateral change to the
signed-string format, key derivation, or encoding turns CI red:

- Wakaru: `backend/tests/test_cart_recovery_hmac.py::test_signature_cross_repo_vector`
- engine: `services/mirofish_hmac_test.go::TestWakaruSignature_CrossRepoVector`

```
secret    = "contract-test-secret"
timestamp = "1700000000"
body      = {"customer_id":"c1"}
signature = 85e86a9154397e23b9d3be5a059982f527d811da061e42ea082ae7471ae0c49d
```

### Deferred

- `merchant_id` binding in the signed payload — lands with multi-tenancy (#24).
