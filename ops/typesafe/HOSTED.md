# Hosted TypeSafe staging

This repository supplies an isolated Railway build using `Dockerfile.hosted` and `railway.hosted.toml`. It does not change the production deployment recipe.

The coordinated runtime profile, resource inventory, volume ownership, activation, rollback and evidence procedure are documented in the Engine repository at `ops/typesafe/HOSTED.md`. The acceptance report is `docs/typesafe-hosted-acceptance-2026-09-23.md` in that repository. Use explicit staging service/environment IDs and the matching reviewed profile. Preserve OpenRouter and both pre-launch fences. Do not add shopper delivery credentials or deploy a sender.
