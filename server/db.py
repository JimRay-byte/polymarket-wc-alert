"""
server/db.py
============
SQLite 持久化。所有提醒写入本地数据库，避免重启丢失；
同时提供历史查询接口供 API / 调试用。
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import List, Optional

from models import Alert


class AlertDB:
    """线程安全（单事件循环内）的 SQLite 封装。"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        # check_same_thread=False：我们用 asyncio.Lock 保证串行
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    match_name TEXT,
                    market_question TEXT,
                    outcome_name TEXT,
                    current_price REAL,
                    implied_prob REAL,
                    estimated_usd REAL,
                    underdog_score REAL,
                    reason TEXT,
                    price_change_short REAL,
                    volume_change_short REAL,
                    market_url TEXT,
                    payload_json TEXT
                );
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_ts ON alerts(timestamp);")
            c.execute("CREATE INDEX IF NOT EXISTS idx_alerts_level ON alerts(level);")

    async def insert(self, alert: Alert) -> int:
        async with self._lock:
            with self._conn() as c:
                cur = c.execute(
                    """INSERT INTO alerts
                       (timestamp, level, match_name, market_question, outcome_name,
                        current_price, implied_prob, estimated_usd, underdog_score,
                        reason, price_change_short, volume_change_short, market_url, payload_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        alert.timestamp, alert.level, alert.match_name,
                        alert.market_question, alert.outcome_name,
                        alert.current_price, alert.implied_prob,
                        alert.estimated_usd, alert.underdog_score,
                        alert.reason, alert.price_change_short,
                        alert.volume_change_short, alert.market_url,
                        json.dumps(alert.to_dict(), ensure_ascii=False),
                    ),
                )
                return int(cur.lastrowid)

    async def recent(self, limit: int = 50, level: Optional[str] = None) -> List[dict]:
        async with self._lock:
            with self._conn() as c:
                if level:
                    rows = c.execute(
                        "SELECT payload_json FROM alerts WHERE level=? "
                        "ORDER BY id DESC LIMIT ?", (level, limit)
                    ).fetchall()
                else:
                    rows = c.execute(
                        "SELECT payload_json FROM alerts ORDER BY id DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
        return [json.loads(r[0]) for r in rows]
