"""Stage 4 normalization service.

Step 6 ships the reusable, deterministic field normalizers in
:mod:`.normalizers` and the vendored ISO 4217 allow-list in :mod:`.iso4217`.
Later steps add the attempt engine, repository, and lifecycle. Nothing in this
package makes an AI or external-network call.
"""
