# Shopper and merchant knowledge v1

Status: local domain-contract slice. No producer, database, HTTP route, worker,
retrieval provider or execution path is connected. These files are deliberately
outside the qualified runtime allowlist. Existing intelligence v1/v2 and recovery
contracts are unchanged. This namespace describes longitudinal knowledge, not a
new recovery-plan version.

`schema.json` is the canonical Draft 2020-12 wire schema. The reference reader is
`cart_recovery.domain.knowledge.read_knowledge`. Both schema validation and its
semantic binding checks are required. Schema validation alone cannot validate
authority, lineage, tenant identity or contradictions.

## Admission boundary

Each UTF-8 JSON batch is one aggregate with exactly one Engine-issued merchant,
store and subject binding. Objects and links inherit that scope; nested scope
overrides are rejected. Merchant subjects use the merchant ID. Shopper subjects
are Engine-resolved IDs within that store. Identity revision and privacy
generation are required positive integers. Missing identity is not guessed from
email, browser similarity, Zep IDs or shopper behavior.

The caller supplies these trusted inputs separately from the candidate JSON:

| Input | Required origin and checks |
| --- | --- |
| `scope` | Authenticated Engine identity/store binding and current privacy generation. Never copy it from the candidate. |
| `sources` | Application-owned source receipts, resolved by stable ID and exact revision. Include source hash, observed time, authority and the exact typed claims approved by deterministic source capture or human review. Verify content hashes against retained source content before issuing receipts. |
| `entities` | Engine/catalog-resolved product, category, brand and policy references, each carrying matching merchant and store IDs. |
| `at` | Timezone-aware server time, never candidate time. |

The reader cannot authenticate the caller or verify source content absent from
the receipt. A future API must obtain these inputs from trusted services inside
the authorization boundary. Exposing this function by accepting those arguments
from an HTTP body would bypass the contract. The fixtures are synthetic receipts,
not proof of live source capture or provider behavior.

Limits: 200,000 wire bytes, 64 objects, 128 links, 64 source/input references per
object, 256 trusted source receipts and 256 catalog receipts. IDs, text and
numbers are bounded. Unknown fields, duplicate JSON keys, non-finite/unsafe
numbers, unsupported versions, malformed times and non-UTF-8 bytes fail closed.
Timestamps use UTC `Z`. No network calls occur during admission.

## Objects and authority

| Kind | Meaning | Required evidence |
| --- | --- | --- |
| `observation` | An immutable event reference, not a personality trait | Exact Engine-observed claim and episode ID |
| `merchant_knowledge` | An approved scoped policy/product/brand claim | Exact merchant-approved claim, catalog reference and effective interval |
| `explicit_preference` | A stated preference with a narrow dimension and applicability | Exact shopper-statement claim; no promotion of behavioral observations |
| `inferred_preference` | A hypothesis, never explicit fact | Included current observations, support links, exact union of source refs, rule/model version and mandatory expiry |
| `episode_state` | A temporary checkout constraint or friction | Exact Engine observation or shopper statement, episode ID and mandatory expiry |
| `relationship` | Shopper interest in a catalog entity, or product governed by policy | Exact evidenced claim and checked endpoint kinds/identities |
| `outcome` | An observed purchase/open/click | Exact Engine event and `observed_only` attribution |
| `unknown` | Missing, unavailable, uncertain or conflicting knowledge | Explicit reason; no invented source or claim |

The envelope has immutable `object_id`, stable `assertion_id`, revision, status,
observed/recorded time, validity interval, expiry, source references and optional
derivation. A new revision has a new object ID. Observed source time is preserved;
recording may happen later. Scheduled and historical records may be admitted.
Admission does **not** mean a record is valid at the current time or usable in a
decision. Context compilation must filter time, withdrawal, identity and privacy
fences before use. It must repeat those checks before execution.

Communication channel is a preference dimension, not permission. There is no
consent, eligibility, offer authorization, send authorization or confidence field
in this contract. Engine retains deterministic authority. Hypothesis admission
checks evidence lineage, not whether the inference is scientifically supported;
that requires the later quality/evaluation gate. Outcomes do not claim causal
conversion lift. Judgment/replay/accounting records retain their existing
separate contracts and lifecycle.

## Relationships, conflicts and correction

Object edges are `supports`, `contradicts` and `supersedes`. All endpoints must be
included in the same bounded aggregate; dangling links and self-links fail.
Support links connect included observations to a derived preference. Evidence
cannot be revoked/superseded, recorded after derivation or expire before it.

Overlapping current claims with the same preference dimension/applicability,
merchant topic/applicability or episode constraint require a contradiction edge
when their values differ. Both records are returned unchanged. No last-write-wins
choice or automatic promotion occurs. The compiler must preserve/resolve the
conflict explicitly; this reader does not decide which claim wins. Unknown with
`conflicting_evidence` alone cannot replace those records.

Supersession requires the same assertion, kind and claim key, consecutive
revision, ordered recording times and a predecessor marked superseded. Every
superseded record has one successor; every revision above one has an included
predecessor. Cycles and forks fail through these constraints. These are portable
fixture checks, not concurrency guarantees. Historical chains exceeding the
batch limit will need a bounded, store-verified lineage receipt in a future
contract revision, not an unbounded batch or silently omitted predecessor.

`revoked` represents a record state, not an erase command. Source withdrawal,
authorization to correct/revoke, atomic uniqueness, downstream invalidation and
restore quarantine remain durable-lifecycle work. No source/personal data is
deleted by this slice.

## Verification and next gate

Run from the repository root with the existing core/test dependencies:

```sh
python contracts/knowledge/v1/verify.py
python -m pytest tests/test_knowledge_contract.py
```

The manifest binds schema, fixtures, reference reader and documentation. Fixtures
cover explicit, inferred, conflicting, corrected, revoked and unknown knowledge;
shopper/merchant relationships; episode constraints; observed outcomes; and
channel-preference separation. Negative tests exercise source, tenant, catalog,
identity, privacy-generation, lineage and wire refusals. A fresh-process check
blocks network calls and rejects simulation/provider/runtime imports.

See `docs/knowledge-ownership.md` for existing owner mappings and the required
durable-lifecycle slice. Runtime packaging and cross-repository contract copying
must occur only when an actual consumer is connected and requalified. Passing
this suite does not extend the previous AMD64 image qualification to new images.
