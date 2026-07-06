# receipt-intake — project guide

A **business-agnostic receipt-capture service**: a receipt photo emailed in → extracted → matched to a job → filed as a structured expense in the target system. Ports + adapters; the core is reusable across deployments. Part of the `usebessemer` stack (a capture front-end for `agent-classes` / `bookkeeper-ui`).

## Architecture — this is the whole point, keep it clean

An agnostic core orchestrates the pipeline through four **ports** (`core/ports.py`): `InboxSource → Extractor → JobResolver → ExpenseSink` (+ `ReviewQueue` for anything the pipeline can't confidently place). **Adapters** implement them: this repo ships `GmailInbox`, `ClaudeExtractor`, `GmailReviewQueue`, and stubs for the sink/resolver seam. Target-system adapters (the store side) live with their deployments, not here — the core must never import a concrete adapter, and no adapter may leak target-system concepts into `core/`.

- Money discipline: `amount` = pre-tax subtotal, `tax` separate; refunds signed negative on both.
- Fail-safe: an item is captured or routed to review — never silently dropped, never confidently misfiled. Review-routed items are marked processed immediately (no re-poll loops).
- Config via `.env` only (`config.py`); secrets never in code, logs, or commits.

## Task intake (dev leaf)

On launch with `begin`: fetch **the sole issue labeled `dev-ready`** (`gh issue list --label dev-ready`) and work it. Coordinate on the substrate, never the human: your report is the PR description; questions/flags are issue/PR comments. Surface, don't absorb — anything out of scope gets filed as an issue, not silently fixed or expanded into.

## Conventions

- Branch flow: `feature/* → develop → main`; `develop → main` at version cuts only (the release boundary is the human's).
- One change at a time; test before commit (`pytest` from repo root); full-file output.
- The leaf never self-merges. The stream lead reviews against the issue's acceptance criteria and merges `feature/* → develop` on green.
- **No `Co-Authored-By: Claude` trailer on any commit. No exceptions.**
- Spec-driven: a change that alters pipeline behavior updates the README "Design" section in the same PR; non-obvious calls get a terse entry in `docs/DECISIONS.md` (decision + why), referenced from the PR.

## Oversight

This repo is overseen by the Bessemer OSS stream lead — briefs arrive as `dev-ready` issues; reviews arrive on the PR.
