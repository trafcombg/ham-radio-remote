"""Creates or updates an admin user. Run from the repo root:
python -m server.create_admin <username> <password>
"""

import asyncio
import json
import sys
from pathlib import Path

from server.db import PostgresDb


async def main():
    if len(sys.argv) != 3:
        print("usage: python -m server.create_admin <username> <password>")
        raise SystemExit(1)
    username, password = sys.argv[1], sys.argv[2]

    cfg = json.loads(Path(__file__).with_name("config.json").read_text(encoding="utf-8"))
    dsn = (cfg.get("db") or {}).get("dsn")
    if not dsn:
        print("server/config.json: db.dsn не е зададен")
        raise SystemExit(1)

    db = PostgresDb(dsn)
    await db.connect()
    await db.create_user(username, password, is_admin=True)
    print(f"admin потребител {username!r} създаден/обновен")


if __name__ == "__main__":
    asyncio.run(main())
