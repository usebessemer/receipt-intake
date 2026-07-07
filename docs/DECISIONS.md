# Design decisions

> **Provenance.** This log is inherited from the service's first production deployment (a private client instance whose target system is a job-costing app on Postgres (“the target system” below)). Entries are preserved as dated rationale — the *why* behind the core's design — even where they describe that instance's adapters, which live in the private deployment, not this repo.

Running log of the non-obvious choices in this service and the *why* — so they don't get re-litigated, and so reviewers and future devs (and the project owner's own learning) have the reasoning. **Append a short entry whenever you make a non-obvious call, and reference it in your PR.** Keep entries terse: decision + why.

## Structured output via forced tool use (not text parsing)
The extractor constrains Claude to call a fixed-schema tool (`tool_choice: {type: tool}`), so the result is schema-validated, not parsed from freeform text. Reliability over cleverness; no regex on prose.

## `amount` is the pre-tax subtotal; tax is stored separately
`ExtractedExpense.amount` = subtotal before tax; `tax` = the HST. Total = `amount + tax`.
**Why (job costing):** HST is a *reclaimable pass-through* — the business claims it back as an input tax credit, so it isn't a true cost of the job. Costing a job at the tax-inclusive total would overstate cost and understate margin. So jobs are costed **pre-tax**, with tax tracked separately for the reclaim.
**Implication for the sink (#3):** store the **pre-tax amount** in the target system cost field and the tax in the new `tax` column. Do *not* store the tax-inclusive total as cost.

## Image formats: detect, don't assume
Receipts come from phones. Detect the image type from the bytes and set the API `media_type` accordingly (jpeg/png/webp/gif). **HEIC** (iPhone default) is not accepted by the API — convert it, or route the item to review with a clear reason. Never assume JPEG.

## `ExpenseKind` enum drives sink routing
The extractor classifies each receipt into one `ExpenseKind`. The sink routes on it: `material_purchase` → the target system `material_purchases`; everything else (`transport`/`catering`/`equipment`/`rental`/`other`) → `production_expenses` with that category. The enum lives in the agnostic core because the categories are generic business-expense types, not the target system-specific names.

## Fail safe via exceptions → review, not junk rows
A receipt missing a required field (merchant/amount/date/kind) raises `ValueError`. The orchestrator catches per-item exceptions, logs, and continues (one bad receipt never aborts the batch); the item routes to the review queue (#7). We never store a half-blank "expense" and never crash the run. A misfiled or junk row is as bad as a lost receipt.

## Direct DB writes to the target system for v1
The sink writes the target system's Postgres directly — fast, and the row shows in the client's live dashboard immediately (tallies are computed from rows). Hardening to the target system's API (its validation, service auth) is a later call, not v1.

## JobResolver: fuzzy matching with confidence threshold
The JobResolver matches receipts to the target system's existing `projects` by fuzzy-matching the email subject against project titles (using `rapidfuzz.fuzz.token_set_ratio`). **Confidence threshold: 80%** — below this, the match is rejected and the item routes to review (#7). **Why:** Email subjects are freeform and typo-prone (e.g., "acmee" for "Acme"); fuzzy matching catches near-misses. But a wrong match (filing a receipt in the wrong job) is worse than no match (routes to review), so we set a high bar (80%) to avoid confidently wrong guesses. Active projects only; test projects (titles containing "test"/"demo"/"temp") are excluded.

## JobResolver port includes subject hint
The `JobResolver.resolve` port takes both the `ExtractedExpense` (merchant, amount, etc.) and a `subject: str` job-name hint. **Why:** the email subject is the primary signal for job matching, but it's not part of the expense data (it's metadata from the inbox). Threading it through the port keeps the contract clear and agnostic — the port doesn't know "subject" is an email field, just that it's a job hint string.

## Dev/test runs against a LOCAL CLONE of the target system, never prod
Production the target system runs the client's live business. All dev, testing, writes, and schema migrations target a **local clone** — `pg_dump` of prod restored into a local Postgres (Docker/Colima on port 5433); the dev `.env`'s the DB-URL config var points at the clone, not prod. Prod is touched only at controlled, backed-up deploys (e.g. applying the #8 tax migration after it's proven on the clone). Reset the sandbox anytime by re-restoring the dump. **Never repoint dev writes at prod.**

## Receipt images stored locally in v1; config-driven for future swaps
The ExpenseSink (#3) stores receipt images to a local directory (controlled by `RECEIPT_IMAGE_STORAGE_PATH` config, default `./receipts`). v1 is filesystem; prod will swap to GCS via config change, no code change. **Why:** decouple storage from business logic; a local directory proves the sink works end-to-end before GCS/auth complexity. This is the "one thing we build fast and prove, then harden later" pattern.

## Receipt image filename: hash image content, not metadata
Filename is derived from `md5(image_bytes)[:8]`, not merchant+date. **Why:** same merchant, same day, same job = different images, but metadata collision → second receipt overwrites first (lost receipt). Content hash is unique per image and dedupes identical re-sends (same receipt emailed twice = same file, no duplicate insertion). A lost receipt is as bad as a half-blank row — can't lose a receipt in the filename scheme.

## Gmail OAuth: load existing token, don't re-derive
The GmailInbox adapter (#2) loads the OAuth token from `GMAIL_TOKEN_PATH` (secrets/gmail_token.json) using `Credentials.from_authorized_user_file()`. It refreshes expired tokens and falls back to installed-app flow only if the token is absent. **Why:** OAuth consent is a human step (opens browser, requires approving scopes); fabricating or re-deriving it breaks the principle of "flag what needs human input, don't fake it." The deployment's OAuth token already exists with scope `gmail.modify` (Production app); loading it avoids a second consent flow. The adapter's contract with the core never mentions Gmail or OAuth — it's fully encapsulated in the adapter.

## Mark uncertain/failed items as processed (no re-loop)
The Orchestrator (#7) marks items as processed **immediately** when routing to review, for both failure cases: (1) extraction failed (ValueError), (2) no confident job match (resolve returns None). **Why:** without this, failed items stay unread and get re-fetched on every poll — burning Claude credits on re-extraction and re-querying the target system on every cycle (hidden infinite loop). Once an item enters the review queue, it's **captured** (not lost, not looped). A misfiled receipt is bad; an infinite re-loop of a failed receipt is worse. The core Orchestrator routes to the ReviewQueue port (adapter-independent), the adapter (GmailReviewQueue) applies a "needs-review" label so reviewers surface items from the inbox.

## Review surfaced via Gmail label (not active notification, v1 scope)
The GmailReviewQueue adapter (#7) applies the "needs-review" label to flagged messages. v1 scope: the label is the discovery mechanism (items appear in the labeled inbox folder). **Why:** active notification (email/SMS/webhook) requires additional auth (gmail.send scope / SMTP / third-party APIs) that's beyond v1. The core guarantee — "never re-loop a failed receipt" — is met by mark_processed. A follow-up issue will wire email/webhook notification; for now, the label surfaces items for review.

## Tax column is schema-tracked in the target system (Drizzle), nullable, no backfill
The `tax` column on `material_purchases` / `production_expenses` is added in the **target-system repo** (`shared/models/projects.ts`, Drizzle), applied via `db:push` — the prod-deployable version of the ad-hoc `ALTER` the sink used on the sandbox in #3. **Nullable, no backfill:** historical rows stay `NULL` (we never saw their HST); tax fills in going forward as the pipeline writes. Prod is a separate controlled, backed-up deploy after it's proven on the clone — never migrate prod from dev. (The the target system change is its own PR in that repo, not receipt-intake.)

## Per-project HST total spans BOTH expense tables, NULL tax = 0
The HST report (the private tax-report adapter) totals `tax` per project across `material_purchases` **and** `production_expenses` (UNION ALL, grouped by project), because a job accrues reclaimable HST on supplies and production costs alike. **NULL tax counts as 0** (`COALESCE`) so pre-pipeline history doesn't break the sum — a project with no captured tax reports 0, not an error. The helper reuses the shared target-system DB pool; it reads existing the target system tables and stays in an adapter (the agnostic core has no concept of HST or the target system schema).

## Entrypoint closes TWO DB pools; startup log names targets, never secrets
The service entrypoint (`main.py`, #20) wires all adapters + the Orchestrator from `load_config()` and runs single-poll or loop mode. **Two non-obvious calls:** (1) **shutdown closes two pools** — the the private job-resolver adapter owns its own asyncpg pool (`resolver.close()`) while the sink and HST report use the shared shared module pool (`close_pool()`); both must close or a SIGTERM leaks connections. (Collapsing the resolver onto the shared pool is a future cleanup; until then, close both.) (2) **the startup banner and `db_target()` surface only name@host:port** parsed via `urlsplit`, deliberately dropping any user/password in the URL — an operator can confirm "pointed at `db@localhost:5433`" (the clone, not prod) without a secret ever reaching the logs. Loop mode's sleep is interruptible (`wait_for(stop_event, timeout=interval)`) so a shutdown signal wakes it immediately instead of waiting out the full interval.

## Refunds/returns/credits are signed NEGATIVE (amount and tax)
The extractor (#15) signs a refund, return, or credit **negative** — both `amount` and `tax`. **Why:** a refund is a credit against the job (it reduces cost), and it reduces the reclaimable HST too, so the tax must carry the same sign. Recording a return as positive spend is the exact error the cost layer must avoid (an early live run extracted a store refund as positive instead of negative). The model is prompted on the cues — "REFUND"/"RETURN"/"CREDIT"/"VOID", negative totals, parenthesised amounts like ($22.19) — and the tool-schema descriptions reinforce the signing. **Belt-and-suspenders:** `_build_expense` then forces the tax sign to follow the amount sign, so a model slip on one sign can't desync amount and tax. The sink + the target system store negatives fine (plain numerics); a negative row naturally reduces that project's budget tally.

## Fold typographic punctuation before fuzzy matching
The JobResolver (#16) folds smart quotes (`’ ‘ “ ”`), en/em dashes (`– —`), and similar to ASCII (`' " -`) in `_normalize`, applied to **both** the subject and project titles before `token_set_ratio`. **Why:** in the 2026-06-05 live run, subject "a real project" with a curly apostrophe scored only **83%** against the "a real project" project (straight quote) — folding makes it **100%**. A curly-vs-straight quote (phones autocorrect apostrophes; project titles are typed straight) shouldn't cost ~17 points and risk a clean subject near-missing the 80% threshold into a false review. Punctuation is normalized away, not matched on.
