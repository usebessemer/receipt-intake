"""Core orchestrator: Inbox → Extract → Resolve → Store pipeline.

Adapter-agnostic orchestration. For each inbox item, extracts → resolves job →
stores (or routes uncertain items to review). Failed items are marked processed
to prevent re-looping.
"""

import logging

from core.ports import (
    Expense,
    ExpenseSink,
    Extractor,
    ExtractedExpense,
    InboxItem,
    InboxSource,
    JobResolver,
    ReviewQueue,
)

logger = logging.getLogger(__name__)


class Orchestrator:
    """Runs the end-to-end receipt intake pipeline."""

    def __init__(
        self,
        inbox: InboxSource,
        extractor: Extractor,
        job_resolver: JobResolver,
        sink: ExpenseSink,
        review_queue: ReviewQueue,
    ):
        self.inbox = inbox
        self.extractor = extractor
        self.job_resolver = job_resolver
        self.sink = sink
        self.review_queue = review_queue

    async def run(self) -> None:
        """Fetch inbox items, extract, resolve jobs, store."""
        items = await self.inbox.fetch_items()
        for item in items:
            await self._process_item(item)

    async def _process_item(self, item: InboxItem) -> None:
        """Process one inbox item through the pipeline.

        On any failure (extraction or job resolution), routes to review queue
        and marks processed to prevent re-looping.
        """
        extracted: ExtractedExpense | None = None

        try:
            # Extract expense data from image
            extracted = await self.extractor.extract(item.image_bytes, item.subject)

            # Resolve to a job
            job_id = await self.job_resolver.resolve(extracted, item.subject)

            if job_id is None:
                # No confident job match — route to review, mark processed
                reason = f"No job match for subject: {item.subject}"
                logger.info(f"Unmatched receipt: {item.subject} → review queue")
                await self.review_queue.submit(item, reason, partial=extracted)
                await self.inbox.mark_processed(item.inbox_id)
                return

            # Success path: store the expense
            expense = Expense(
                job_id=job_id,
                merchant=extracted.merchant,
                amount=extracted.amount,
                tax=extracted.tax,
                date=extracted.date,
                kind=extracted.kind,
                description=extracted.description,
                image_bytes=item.image_bytes,
            )
            await self.sink.store(expense)
            await self.inbox.mark_processed(item.inbox_id)
            logger.info(f"Stored {item.subject} to job {job_id}")

        except Exception as e:
            # Extraction or storage failure — route to review, mark processed
            reason = f"Processing failed: {str(e)}"
            logger.error(f"Failed to process {item.subject}: {e}")
            await self.review_queue.submit(item, reason, partial=extracted)
            await self.inbox.mark_processed(item.inbox_id)
