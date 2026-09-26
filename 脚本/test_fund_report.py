#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fund_report.py 的 XIRR 单元测试
================================
已知现金流 → 已知 XIRR 的验证。

运行:
    python 脚本/test_fund_report.py
    pytest 脚本/test_fund_report.py   # 如果安装了 pytest
"""

import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from fund_report import xirr


class TestXIRR(unittest.TestCase):

    def test_simple_annual(self):
        """一年前投入 -100，今天收回 110 → XIRR ≈ 10%"""
        flows = [(date(2025, 1, 1), -100), (date(2026, 1, 1), 110)]
        result = xirr(flows)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.10, places=2)

    def test_multi_year_irregular(self):
        """跨3年不规则现金流 → 正收益"""
        flows = [
            (date(2023, 1, 1), -1000),
            (date(2024, 7, 1), -2000),
            (date(2026, 1, 1), 4500),
        ]
        result = xirr(flows)
        self.assertIsNotNone(result)
        # 投入3000收回4500 → 正收益
        self.assertGreater(result, 0)
        self.assertAlmostEqual(result, 0.2182, places=2)

    def test_all_negative_returns_none(self):
        """全部负现金流 → 无解，返回 None"""
        flows = [(date(2025, 1, 1), -100), (date(2026, 1, 1), -50)]
        self.assertIsNone(xirr(flows))

    def test_all_positive_returns_none(self):
        """全部正现金流 → 无解，返回 None"""
        flows = [(date(2025, 1, 1), 100), (date(2026, 1, 1), 50)]
        self.assertIsNone(xirr(flows))

    def test_single_cashflow_returns_none(self):
        """单笔现金流 → 不足2笔，返回 None"""
        flows = [(date(2025, 1, 1), -100)]
        self.assertIsNone(xirr(flows))

    def test_zero_cashflow_pair(self):
        """一正一负但金额对称 → XIRR ≈ 0"""
        flows = [(date(2025, 1, 1), -100), (date(2025, 7, 1), 100)]
        result = xirr(flows)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result, 0.0, places=2)

    def test_negative_return(self):
        """亏损场景 → XIRR < 0
        -100 on 2025-01-01, +80 on 2026-01-01 → XIRR ≈ -20%
        """
        flows = [(date(2025, 1, 1), -100), (date(2026, 1, 1), 80)]
        result = xirr(flows)
        self.assertIsNotNone(result)
        self.assertLess(result, 0)
        self.assertAlmostEqual(result, -0.20, places=2)

    def test_many_cashflows_brentq(self):
        """多笔定投 → brentq 应能求解"""
        flows = [
            (date(2024, 1, 1), -500),
            (date(2024, 2, 1), -500),
            (date(2024, 3, 1), -500),
            (date(2024, 4, 1), -500),
            (date(2024, 5, 1), -500),
            (date(2024, 6, 1), -500),
            (date(2025, 6, 1), 3500),
        ]
        result = xirr(flows)
        self.assertIsNotNone(result)
        # 6 * 500 = 3000 invested, 3500 received → positive XIRR
        self.assertGreater(result, 0)

    def test_same_day_zero_xirr(self):
        """同日一正一负 → 隐含 XIRR=0，但 brentq 区间可能无法定位；
        此测试仅验证函数不崩溃（返回值可能是 None 或 0.0）"""
        flows = [(date(2025, 1, 1), -100), (date(2025, 1, 1), 100)]
        result = xirr(flows)
        # 同日现金流 NPV = 0 for all r → brentq 无法找到区间，
        # 但 Newton 法可能收敛到 0。接受 None 或 ≈0。
        if result is not None:
            self.assertAlmostEqual(result, 0.0, places=4)


if __name__ == "__main__":
    unittest.main()