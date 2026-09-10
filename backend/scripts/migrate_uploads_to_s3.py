"""One-time, idempotent migration of local original PDFs into S3 / Cloudflare R2.

For every ``documents`` row it reads ``<UPLOAD_DIRECTORY>/<file_location>``,
uploads it to the configured S3 bucket **at the same key**, and verifies the
uploaded bytes' size and SHA-256 against the row's stored ``file_hash``. It
makes **no database writes** - ``file_location`` already is the object key.

Safe to re-run: an object already present with matching bytes is skipped. A
row whose local file is missing or whose hash disagrees is reported and left
untouched (exit code 1 if any such row is found).

Usage (from ``backend/``), with the deployed S3 configuration exported and
``STORAGE_BACKEND=s3``::

    uv run python -m scripts.migrate_uploads_to_s3 [--dry-run] [--local-dir PATH]

``--dry-run`` reports what would happen and uploads nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from dataclasses import dataclass, field

from sqlalchemy import select

from app.core.config import settings
from app.database.session import SessionLocal
from app.models.document import Document
from app.services.storage import StorageError, build_storage
from app.services.storage.local import LocalFileStorage
from app.services.storage.s3 import S3FileStorage


@dataclass
class Report:
    uploaded: list[str] = field(default_factory=list)
    skipped_present: list[str] = field(default_factory=list)
    missing_local: list[str] = field(default_factory=list)
    hash_mismatch: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.missing_local and not self.hash_mismatch

    def render(self) -> str:
        lines = [
            f"uploaded:        {len(self.uploaded)}",
            f"already present: {len(self.skipped_present)}",
            f"missing local:   {len(self.missing_local)}",
            f"hash mismatch:   {len(self.hash_mismatch)}",
        ]
        for label, rows in (
            ("MISSING LOCAL", self.missing_local),
            ("HASH MISMATCH", self.hash_mismatch),
        ):
            for row in rows:
                lines.append(f"  {label}: {row}")
        return "\n".join(lines)


async def _run(*, dry_run: bool, local_dir: str | None) -> Report:
    storage = build_storage(settings)
    if not isinstance(storage, S3FileStorage):
        raise SystemExit(
            "STORAGE_BACKEND must be 's3' (with the S3 settings) to run this migration."
        )
    local = LocalFileStorage(local_dir or settings.upload_directory)
    report = Report()

    async with SessionLocal() as session:
        rows = (await session.execute(select(Document))).scalars().all()

    for document in rows:
        ident = f"{document.document_id} ({document.file_location})"
        try:
            data = await local.get_bytes(document.file_location)
        except StorageError:
            report.missing_local.append(ident)
            continue

        digest = hashlib.sha256(data).hexdigest()
        if digest != document.file_hash:
            report.hash_mismatch.append(ident)
            continue

        if await storage.exists(document.file_location):
            # Never trust existence (or size) alone: a corrupt/wrong object can
            # have the same length as the source. Verify the persisted row's
            # authoritative digest before treating a rerun as successful.
            existing = await storage.get_bytes(document.file_location)
            if hashlib.sha256(existing).hexdigest() != document.file_hash:
                report.hash_mismatch.append(f"{ident} [existing object differs]")
                continue
            report.skipped_present.append(ident)
            continue

        if dry_run:
            report.uploaded.append(f"{ident} [dry-run]")
            continue

        stored = await storage.save_bytes(document.document_id, data)
        # Read back and re-verify before counting it done.
        roundtrip = await storage.get_bytes(stored.location)
        if hashlib.sha256(roundtrip).hexdigest() != document.file_hash:
            report.hash_mismatch.append(f"{ident} [post-upload verify failed]")
            continue
        report.uploaded.append(ident)

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report only, upload nothing")
    parser.add_argument(
        "--local-dir",
        default=None,
        help="override the local upload directory (defaults to UPLOAD_DIRECTORY)",
    )
    args = parser.parse_args(argv)

    report = asyncio.run(_run(dry_run=args.dry_run, local_dir=args.local_dir))
    print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
