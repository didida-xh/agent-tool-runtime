"""按文件名顺序执行 migrations/*.sql。

所有迁移都写成幂等语句，重复执行安全。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


async def main() -> None:
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        for sql_file in sorted((ROOT / "migrations").glob("*.sql")):
            await conn.execute(sql_file.read_text(encoding="utf-8"))
            print(f"applied {sql_file.name}")
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
