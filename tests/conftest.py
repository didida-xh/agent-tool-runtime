"""pytest 公共装置。

1. 加载 .env：db.py 用 os.environ["DATABASE_URL"]，pytest 不会自动读 .env；
2. 按用例关闭 asyncpg 连接池：pytest-asyncio 每个用例一个新事件循环，
   连接池跨轮复用会报 "attached to a different loop"。
"""

from pathlib import Path

import pytest
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

import app.db as db  # noqa: E402


@pytest.fixture(autouse=True)
async def _isolate_db_pool():
    yield
    pool = getattr(db, "_pool", None)
    if pool is not None:
        await pool.close()
        db._pool = None
