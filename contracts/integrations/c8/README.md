# C8 private bounded input

The canonical reference is Engine `docs/integration-bounded-context-c8.md`. This directory adds an executable three-language handoff fixture without changing the C1 schema bundle. `fixture.json` is identical in Engine, Wakaru and Inkwell and pins canonical request/plan hashing plus approved-copy authority.

The input version is `bounded_context_c8_v1`; the output uses the existing strict `recovery_plan_v2`. Engine and Inkwell use matching `internal/contextcopy` validators. Wakaru uses `cart_recovery.intelligence.bounded_context`. Cross-repository CI tests actual producer and consumers; it must pin companion commits.

All output is review-only. Public endpoints, live ingestion, model invocation and email sends are disabled for this path. See the reference design for exact bindings, privacy ownership and remaining activation requirements.
