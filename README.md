# receipt-intake

A **business-agnostic receipt-capture service.** Email a receipt photo → it's read, matched to a job, and filed as a structured expense in your target system. One human action (snap + email + job name in the subject); everything else automatic.

Part of the [usebessemer](https://github.com/usebessemer) stack — a capture front-end for the [Bookkeeper agent framework](https://github.com/usebessemer/agent-classes) and its [thin UI](https://github.com/usebessemer/bookkeeper-ui).

## Why

Most small businesses record costs but lose receipts — no expense backup, no tax (HST/VAT) reclaim. This service closes that gap automatically, and receipts land in the system the business already uses.

## How it works

An agnostic core orchestrates the pipeline through four **ports** (interfaces); **adapters** implement them per target system:

```
Inbox → Extract → Resolve job → Store
  │        │          │            │
InboxSource Extractor JobResolver ExpenseSink     ← ports (core/ports.py)
  │            │            │            │
GmailInbox ClaudeExtractor  (yours)    (yours)    ← adapters
```

**Ships in this repo:** the core orchestrator, the Gmail inbox + review-queue adapters, the Claude vision extractor, and stub sink/resolver so the pipeline runs end-to-end out of the box. Anything the pipeline can't confidently place routes to a human review queue (a Gmail label) rather than being guessed at — a receipt is *captured or reviewed, never silently dropped or confidently misfiled*.

**You bring:** an `ExpenseSink` + `JobResolver` for your target system (see `core/ports.py`; wire them in `main.py`). The first production deployment files expenses into a private job-costing database through exactly this seam. An adapter that submits candidates to the Bookkeeper UI's ingest port is planned — see the issue tracker.

## Run it

```bash
pip install -r requirements.txt
cp .env.example .env       # fill in Gmail OAuth paths + Anthropic key
python main.py single-poll # process the inbox once
python main.py loop        # poll until interrupted
```

On startup it logs what it's connected to **by name** — never secrets. `secrets/` and `.env` are gitignored; never commit them.

## Design

The non-obvious calls and their *why* live in [`docs/DECISIONS.md`](docs/DECISIONS.md) (inherited from the first production deployment); the pipeline contract is `core/ports.py` (the four ports are the spec). Highlights: extraction uses forced tool-use (schema-validated, never parsed from prose); `amount` is the pre-tax subtotal with tax stored separately; refunds are signed negative (amount *and* tax); review-routed items are marked processed immediately so a failed item can never burn API credits in a re-poll loop.

## License

MIT.
