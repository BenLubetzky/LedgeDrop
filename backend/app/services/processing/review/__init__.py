"""Stage 7 human-review subsystem.

Package 1: the internal contract (:mod:`app.schemas.review`), the persistence
bridge (:mod:`app.schemas.review_persistence`), the ORM model
(:mod:`app.models.review`), migration ``0006_review_tables``, and
:class:`.ReviewRepository` (the sole reader/writer of ``invoice_reviews``,
plus the review-queue query).

Package 2 (this package's :mod:`.lifecycle` and :mod:`.service`): the guards
that gate a resolution (`DECISION_NOT_REVIEWABLE` / `DECISION_ALREADY_REVIEWED`
/ `STALE_DECISION_SOURCE`) and :class:`.ReviewService`, which locks the target
decision and its owning document and writes the review row plus the terminal
``documents.status`` (``NEEDS_REVIEW -> APPROVED | REJECTED``) in one
transaction. The scoped API routes and the queue endpoint are in
:mod:`app.api.reviews`; the reviewer UI is package 3.
"""

from app.services.processing.review.repository import ReviewRepository
from app.services.processing.review.service import ReviewService

__all__ = ["ReviewRepository", "ReviewService"]
