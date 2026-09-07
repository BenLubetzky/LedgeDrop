import asyncio
import uuid

from app.database.session import SessionLocal
from app.models.document import Document


DOCUMENT_ID = uuid.UUID("0a323b92-664e-4fe1-900e-0d79b39f1570")


async def main() -> None:
    async with SessionLocal() as session:
        document = await session.get(Document, DOCUMENT_ID)
        print("found", document is not None, getattr(document, "status", None))
        if document is None:
            return
        await session.delete(document)
        await session.commit()
        print("database delete committed")


asyncio.run(main())
