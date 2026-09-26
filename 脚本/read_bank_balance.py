#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
银行余额工具（多卡 + 多币别）
============================
按 (卡 id, 币别) 分组：
  - get_opening_balance(period, card_id, currency) → 期初余额
  - get_daily_closing_balances(period, card_id, currency) → 每日末余额

CLI:
  python 脚本/read_bank_balance.py <PERIOD> [card_id]
"""

import sys
from decimal import Decimal
from pathlib import Path
from datetime import date

import pandas as pd

ROOT = Path(__file__).parent.parent

sys.path.insert(0, str(Path(__file__).parent))
from config import BANK_CARDS


def _get_bank_df(period: str, card_id: str, currency: str | None = None) -> pd.DataFrame:
    """读取指定卡的 Excel，按交易时间升序；如指定 currency 则只保留该币别。"""
    path = ROOT / f"整理后数据/{period}/银行卡{card_id}_{period}.xlsx"
    df = pd.read_excel(path)
    df["交易时间"] = pd.to_datetime(df["交易时间"])
    if "币别" not in df.columns:
        df["币别"] = "CNY"
    if currency:
        df = df[df["币别"] == currency]
    df = df.sort_values("交易时间").reset_index(drop=True)
    return df


def get_opening_balance(period: str, card_id: str, currency: str = "CNY") -> Decimal | None:
    """从该 (卡, 币别) 最早一笔反推期初余额。无数据返回 None。"""
    df = _get_bank_df(period, card_id, currency)
    if len(df) == 0:
        return None
    earliest = df.iloc[0]
    amt = float(earliest["金额(元)"])
    bal_after = float(earliest["余额"])
    direction = str(earliest["收/支"]).strip()
    if direction.startswith("支"):
        opening = bal_after + amt
    elif direction.startswith("收"):
        opening = bal_after - amt
    else:
        opening = bal_after
    return Decimal(str(round(opening, 2)))


def _chain_order_ties(df):
    """对「同一交易时间」的并列行，用「余额」链条还原真实先后顺序，返回有序的行 dict 列表。

    银行「余额」列是每笔之后的运行余额（ground truth）。同秒并列时按行序取末笔会
    取到中间值，导致当日末余额错误。这里在每个时间戳分组内贪心重排：下一笔的
    「入账前余额 = 余额 - 带符号金额」应等于上一笔的余额，据此串成正确链。
    """
    df = df.sort_values("交易时间", kind="stable").reset_index(drop=True)
    recs = df.to_dict("records")

    def signed(r):
        amt = abs(float(r["金额(元)"]))
        return -amt if str(r.get("收/支", "")) == "支出" else amt

    ordered_all = []
    prev_bal = None
    i = 0
    while i < len(recs):
        j = i
        while j < len(recs) and recs[j]["交易时间"] == recs[i]["交易时间"]:
            j += 1
        group = recs[i:j]
        if len(group) == 1:
            ordered_all.append(group[0])
            prev_bal = round(float(group[0]["余额"]), 2)
        else:
            remaining = group[:]
            cur = prev_bal
            while remaining:
                pick = None
                if cur is not None:
                    for r in remaining:
                        if round(float(r["余额"]) - signed(r), 2) == cur:
                            pick = r; break
                if pick is None:  # 链断（数据异常）→ 退回原序
                    pick = remaining[0]
                ordered_all.append(pick)
                remaining.remove(pick)
                cur = round(float(pick["余额"]), 2)
            prev_bal = cur
        i = j
    return ordered_all


def get_daily_closing_balances(period: str, card_id: str, currency: str = "CNY") -> list[tuple[date, Decimal]]:
    """每日末余额（按交易时间升序；同秒并列时用余额链还原真实末笔）。"""
    df = _get_bank_df(period, card_id, currency)
    if len(df) == 0:
        return []
    last_per_day: dict = {}
    for r in _chain_order_ties(df):
        last_per_day[r["交易时间"].date()] = round(float(r["余额"]), 2)
    result = [(d, Decimal(str(bal))) for d, bal in last_per_day.items()]
    return sorted(result, key=lambda x: x[0])


def iter_card_currencies(period: str):
    """遍历所有 (card_id, currency) 组合，仅返回 Excel 中实际有数据的。"""
    for card_id, card in BANK_CARDS.items():
        path = ROOT / f"整理后数据/{period}/银行卡{card_id}_{period}.xlsx"
        if not path.exists():
            continue
        df = pd.read_excel(path)
        if "币别" not in df.columns:
            df["币别"] = "CNY"
        for ccy in sorted(df["币别"].dropna().unique()):
            yield card_id, str(ccy)


def _cli(period: str, card_filter: str | None = None) -> None:
    for card_id, ccy in iter_card_currencies(period):
        if card_filter and card_id != card_filter:
            continue
        opening = get_opening_balance(period, card_id, ccy)
        daily = get_daily_closing_balances(period, card_id, ccy)
        print(f"\n========== 卡 {card_id} - {ccy} ==========")
        print(f"期初余额: {opening} {ccy}")
        print(f"每日末余额（{len(daily)} 个交易日）:")
        for d, bal in daily:
            print(f"  {d}  {bal} {ccy}")


if __name__ == "__main__":
    period = sys.argv[1] if len(sys.argv) > 1 else "2507-2508"
    card_filter = sys.argv[2] if len(sys.argv) > 2 else None
    _cli(period, card_filter)
