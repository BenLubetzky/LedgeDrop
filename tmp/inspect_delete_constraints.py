import asyncio

import asyncpg


async def main() -> None:
    connection = await asyncpg.connect(
        "postgresql://ledgerdrop:ledgerdrop@localhost:5432/ledgerdrop"
    )
    rows = await connection.fetch(
        """
        SELECT tc.table_name, tc.constraint_name, rc.delete_rule
        FROM information_schema.table_constraints AS tc
        JOIN information_schema.referential_constraints AS rc
          ON rc.constraint_name = tc.constraint_name
         AND rc.constraint_schema = tc.constraint_schema
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
        ORDER BY tc.table_name, tc.constraint_name
        """
    )
    for row in rows:
        print(dict(row))
    await connection.close()


asyncio.run(main())
