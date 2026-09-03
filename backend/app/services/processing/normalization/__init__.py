"""Stage 4 normalization service.

The deterministic field normalizers live in :mod:`.normalizers` and the
vendored ISO 4217 allow-list in :mod:`.iso4217`. The step 11 attempt engine
(:mod:`.engine`), data access (:mod:`.repository`), lifecycle guards
(:mod:`.lifecycle`), and orchestration (:mod:`.service`) turn a completed Stage
3 extraction into a persisted, traceable normalized result. Nothing in this
package makes an AI or external-network call.
"""

from app.services.processing.normalization.engine import normalize_extraction
from app.services.processing.normalization.repository import NormalizationRepository
from app.services.processing.normalization.service import NormalizationService

__all__ = [
    "normalize_extraction",
    "NormalizationRepository",
    "NormalizationService",
]