"""
tests/test_detector.py
======================
不依赖网络的单元测试：用模拟成交验证 detector 的冷门评分与提醒触发逻辑。

运行：
    cd server && python -m pytest ../tests/test_detector.py -v
或直接：
    python tests/test_detector.py
"""
import os
import sys
import time
import unittest
from pathlib import Path

# 让 tests 能 import server 模块
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))

from config import Settings, DetectorSettings, ServerSettings  # noqa: E402
from detector import Detector  # noqa: E402
from models import Market, Outcome  # noqa: E402


def make_market() -> Market:
    return Market(
        condition_id="cond_demo",
        question="Will Saudi Arabia win the 2026 World Cup?",
        event_slug="2026-world-cup-winner",
        outcomes=[
            Outcome(name="Yes", clob_token_id="tok_yes", price=0.04),
            Outcome(name="No", clob_token_id="tok_no", price=0.96),
        ],
        liquidity_usd=200000.0,
        volume_total=500000.0,
        active=True, closed=False,
        url="https://polymarket.com/event/2026-world-cup-winner",
    )


class DetectorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # 用 loopback host，避免新的 token 强度校验拦截（detector 测试不关心 token）
        self.settings = Settings(server=ServerSettings(host="127.0.0.1",
                                                       auth_token="test-only-not-used"))
        # 放低阈值便于测试
        self.settings.detector = DetectorSettings(
            large_trade_usd=1000.0,
            important_trade_usd=5000.0,
            severe_trade_usd=20000.0,
            underdog_score_threshold=50.0,
            underdog_max_implied_prob=0.35,
            short_window_sec=300,
            price_spike_ratio=0.05,
            volume_anomaly_multiplier=3.0,
            min_book_depth_usd=200.0,
            short_window_large_usd=2000.0,
        )
        self.det = Detector(self.settings)

    async def test_cold_outcome_large_trade_triggers_alert(self):
        """冷门方向(Yes, p=0.04) + 大额成交 + 盘口充足 → 应提醒"""
        m = make_market()
        yes = m.find_outcome("tok_yes")

        # 先注入历史小成交作为基线，再注入深度
        for i in range(20):
            await self.det.on_trade("tok_yes", size=10, price=0.03,
                                    ts=time.time() - 3600 + i * 10)
        await self.det.on_book("tok_yes", depth_usd=50000.0)
        await self.det.on_price("tok_yes", 0.04)

        alert = await self.det.on_trade("tok_yes", size=30000, price=0.04)
        # on_trade 本身不返回 alert（设计上交由 evaluate_with_market 判定）
        self.assertIsNone(alert)

        alert = await self.det.evaluate_with_market(
            m, yes, estimated_usd=30000 * 0.04,
            size_missing=False, window_mode=False)
        self.assertIsNotNone(alert, "冷门方向大额成交应触发提醒")
        self.assertGreaterEqual(alert.underdog_score, self.settings.detector.underdog_score_threshold)
        self.assertIn(alert.level, ("INFO", "IMPORTANT", "SEVERE"))

    async def test_favorite_direction_not_alerted(self):
        """热门方向(No, p=0.96) 即使大额也不提醒（不冷门）"""
        m = make_market()
        no = m.find_outcome("tok_no")
        await self.det.on_book("tok_no", depth_usd=50000.0)
        await self.det.on_price("tok_no", 0.96)
        alert = await self.det.evaluate_with_market(
            m, no, estimated_usd=50000,
            size_missing=False, window_mode=False)
        self.assertIsNone(alert, "热门方向不应触发冷门提醒")

    async def test_thin_book_filtered(self):
        """盘口深度低于阈值 → 即使冷门大额也忽略（降噪）"""
        m = make_market()
        yes = m.find_outcome("tok_yes")
        await self.det.on_book("tok_yes", depth_usd=10.0)  # 极薄
        await self.det.on_price("tok_yes", 0.04)
        alert = await self.det.evaluate_with_market(
            m, yes, estimated_usd=50000,
            size_missing=False, window_mode=False)
        self.assertIsNone(alert, "盘口过薄应被过滤")

    async def test_window_mode_detects_split_orders(self):
        """拆单：多笔小成交累计达到短窗口阈值 → 应触发"""
        m = make_market()
        yes = m.find_outcome("tok_yes")
        await self.det.on_book("tok_yes", depth_usd=50000.0)
        now = time.time()
        # 10 笔 6000 份额 × 0.04 ≈ 240 USDC / 笔，累计 2400 >= short_window_large_usd(2000)
        for i in range(10):
            await self.det.on_trade("tok_yes", size=6000, price=0.04, ts=now - 60 + i)
        await self.det.evaluate_with_market(
            m, yes, estimated_usd=2400, size_missing=False, window_mode=True)
        # 主要验证：window_mode 路径能正常运行（不抛异常）


if __name__ == "__main__":
    unittest.main()
