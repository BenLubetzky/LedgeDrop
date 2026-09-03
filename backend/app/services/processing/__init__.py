"""Stage 3+ processing pipeline: extraction, then normalization, then (later)
validation and decision/escalation.

Extraction (Stage 3) and normalization (Stage 4) each exist as an independent
subsystem with its own service, lifecycle, repository, and API. ``pipeline.py``
composes them into the ``upload -> extraction -> normalization`` chain without
adding processing rules of its own; validation and decision are not built yet.
"""
