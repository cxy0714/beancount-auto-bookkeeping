#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金期初持仓回溯工具
====================
提供两种使用方式：
  1. 作为 module 被 import（推荐）：backcalc_holdings(period, end_holdings_text, start_date)
  2. 独立 CLI（手动修改配置区后运行）：python 脚本/back_fund.py
"""

import re
import os
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ─────────────────────────────────────────────────
# 公共函数（可 import）
# ─────────────────────────────────────────────────

def parse_holdings(raw_text: str) -> dict:
    """
    解析 beancount 持仓文本，返回 {fund_code: {account, units, price}}。
    支持以下格式（来自 init 块或 bean-check 格式）：
      Assets:Invest:Fund:FUNDxxxxxx   N.NNNN FUNDxxxxxx {P.PPPP CNY}
    """
    holdings = {}
    pattern = r"(Assets:Invest:Fund:(FUND\d+))\s+([\d\.]+)\s+FUND\d+\s+\{([\d\.]+)\s+CNY\}"
    for account, code, units, price in re.findall(pattern, raw_text):
        holdings[code] = {
            "account": account,
            "units": Decimal(units),
            "price": Decimal(price),
        }
    return holdings


def backcalc_holdings(
    period: str,
    end_holdings_text: str,
    start_date: str,
) -> str:
    """
    根据期末持仓和期间变动，倒推期初持仓（份额层面）。

    参数：
      period           — 账期字符串，如 "2507-2508"
      end_holdings_text — 期末持仓文本（beancount 格式，含 FUND 代码和 {price CNY}）
      start_date       — 期初日期字符串，如 "2025-06-21"

    返回：
      beancount 格式的期初持仓初始化字符串（不含 trailing newline）
    """
    # 1. 解析期末持仓（延续到下一账期的基金；可能为空）
    end_holdings = parse_holdings(end_holdings_text)

    # 2. 扫描期间 alipay_funds bean 文件，统计所有基金的份额变动
    fund_bean_path = ROOT / "bean_files" / period / f"alipay_funds_{period}.bean"
    if not fund_bean_path.exists():
        raise FileNotFoundError(f"找不到基金账单: {fund_bean_path}")

    with open(fund_bean_path, encoding="utf-8") as f:
        content = f.read()

    # 统计期间内出现的「所有」基金净变动（不仅是延续到下期的）：
    # 账期内清仓的旧基金也必须反推期初，否则其卖出腿没有对应持仓，
    # bean-check 无法按成本配对、份额断言失败。
    deltas: dict[str, Decimal] = {}
    for amount_str, code in re.findall(r"([\-]?\d+\.?\d*)\s+(FUND\d+)", content):
        deltas[code] = deltas.get(code, Decimal("0")) + Decimal(amount_str)

    # 每只基金的代表性成本价：优先用期末持仓的成本；否则取期间账单里该基金的
    # 首个价格（买入成本 {x CNY} 或卖出价 @ x CNY），仅用于期初 lot 的成本标注。
    def _period_price(code: str) -> str:
        m = re.search(rf"{code}\s*\{{\s*([\d.]+)\s*CNY\s*\}}", content)  # 买入 {price CNY}
        if m:
            return m.group(1)
        m = re.search(rf"{code}\s*\{{\}}\s*@\s*([\d.]+)\s*CNY", content)  # 卖出 {} @ price
        if m:
            return m.group(1)
        return "0"

    all_codes = set(deltas) | set(end_holdings)

    # 3. 期初 = 期末 - 期间净变动（期末未持有的基金期末=0）
    lines = [
        f"; === 自动倒推生成的期初初始化 ({start_date}) ===",
        f"; 计算逻辑：期初 = 期末({period}末) - 期间变动",
        f'{start_date} * "初始持仓回溯"',
    ]
    for code in sorted(all_codes):
        info = end_holdings.get(code)
        end_units = info["units"] if info else Decimal("0")
        init_units = end_units - deltas.get(code, Decimal("0"))
        if init_units == 0:
            continue  # 期初无持仓，无需 lot
        account = info["account"] if info else f"Assets:Invest:Fund:{code}"
        price = info["price"] if info else _period_price(code)
        lines.append(
            f"  {account:<45} {init_units:>10.4f} {code} {{{price} CNY}}"
        )
    lines.append(f"  {'Equity:Opening-Balances':<45}")

    return "\n".join(lines)


# ─────────────────────────────────────────────────
# CLI 入口（手动修改配置区后运行，兼容旧用法）
# ─────────────────────────────────────────────────

# --- 配置区 ---
# 1. 贴入你"当前"的、已知的期末持仓（例如 11-21 的余额）
RAW_END_HOLDINGS = """
; === 自动倒推生成的期初初始化 (2025-08-21) ===
; 计算逻辑：期初 = 期末(2509末) - 期间变动
2025-08-21 * "初始持仓回溯" "已核查，份额正确"
  Assets:Invest:Fund:FUND001595                   164.5600 FUND001595 {1.7346 CNY}
  Assets:Invest:Fund:FUND002963                   360.8200 FUND002963 {3.1112 CNY}
  Assets:Invest:Fund:FUND005693                   249.8400 FUND005693 {1.1490 CNY}
  Assets:Invest:Fund:FUND006479                    66.00 FUND006479 {6.8182 CNY}
  Assets:Invest:Fund:FUND007605                   130.8200 FUND007605 {1.3500 CNY}
  Assets:Invest:Fund:FUND050025                    91.6400 FUND050025 {4.9850 CNY}
  Equity:Opening-Balances
"""

# 2. 账期
PERIOD = "2507-2508"

# 3. 期初日期（新 period 的第一天的前一天）
START_DATE = "2025-06-21"
# --------------

def _cli() -> None:
    result = backcalc_holdings(PERIOD, RAW_END_HOLDINGS, START_DATE)
    print(result)


if __name__ == "__main__":
    _cli()
