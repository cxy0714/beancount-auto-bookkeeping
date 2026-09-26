#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
精查某一天工具
==============
用法：
  python 脚本/inspect_date.py <PERIOD> <DATE> [--account ACCT]

  PERIOD  — 账期，如 2507-2508
  DATE    — 日期，如 2025-07-15
  --account / -a  — 只显示涉及该账户的 bean 分录（可选）

输出：
  1. 该日的银行原始记录（来自 Excel）
  2. 该日的支付宝/微信/京东原始记录
  3. 该日的 .bean 分录（涉及指定账户的）
  横向对照，方便人工排查漏记/错记。
"""

import sys
import re
import argparse
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent

# ─────────────────────────────────────────────────────────
# 银行原始记录
# ─────────────────────────────────────────────────────────

def _show_bank(period: str, target: date) -> None:
    excel_dir = ROOT / "整理后数据" / period
    bank_files = sorted(excel_dir.glob(f"银行卡*_{period}.xlsx"))
    if not bank_files:
        print("  （找不到银行 Excel）")
        return

    shown = 0
    for excel_path in bank_files:
        df = pd.read_excel(excel_path)
        df["交易时间"] = pd.to_datetime(df["交易时间"])
        mask = df["交易时间"].dt.date == target
        rows = df[mask]
        if rows.empty:
            continue
        card = excel_path.stem.split("_")[0]
        for _, row in rows.iterrows():
            print(
                f"  [{card}] {row['交易时间']}  {row['收/支']:2}  "
                f"¥{float(row['金额(元)']):>10.2f}  余额 {float(row['余额']):>10.2f}  {row['交易对方']}"
            )
            shown += 1
    if shown == 0:
        print("  （该日无银行交易）")


# ─────────────────────────────────────────────────────────
# 支付宝 / 微信 / 京东原始记录
# ─────────────────────────────────────────────────────────

_EXCEL_MAP = {
    "alipay":   "支付宝整合",
    "alipay_funds": "支付宝基金",
    "wechat":   "微信",
    "jingdong": "京东",
}

def _show_source(period: str, source_key: str, target: date) -> None:
    prefix = _EXCEL_MAP.get(source_key, source_key)
    excel_path = ROOT / f"整理后数据/{period}/{prefix}_{period}.xlsx"
    if not excel_path.exists():
        print(f"  （找不到 {prefix} Excel）")
        return
    df = pd.read_excel(excel_path)
    df["交易时间"] = pd.to_datetime(df["交易时间"])
    mask = df["交易时间"].dt.date == target
    rows = df[mask]
    if rows.empty:
        print(f"  （该日无 {prefix} 记录）")
        return
    for _, row in rows.iterrows():
        amt_col = "金额(元)" if "金额(元)" in df.columns else df.columns[5]
        amt = f"¥{float(row[amt_col]):>10.2f}" if pd.notna(row[amt_col]) else "          "
        payee = row.get("交易对方", "") or row.get("收款方", "")
        goods = row.get("商品", "") or row.get("备注", "")
        print(f"  {row['交易时间']}  {str(row.get('收/支', '')):3}  {amt}  {payee}  {goods}")


# ─────────────────────────────────────────────────────────
# Bean 分录
# ─────────────────────────────────────────────────────────

def _show_bean_entries(period: str, target: date, account_filter: str | None = None) -> None:
    bean_dir = ROOT / "bean_files" / period
    date_str = str(target)
    date_pat = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+[*!]')
    found_any = False

    for fpath in sorted(bean_dir.glob("*.bean")):
        with open(fpath, encoding="utf-8") as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            m = date_pat.match(lines[i])
            if m and m.group(1) == date_str:
                # 提取完整事务块
                block = [lines[i]]
                j = i + 1
                while j < len(lines):
                    ln = lines[j]
                    if ln.strip() == "" or ln.startswith(" ") or ln.startswith("\t"):
                        block.append(ln)
                        j += 1
                    else:
                        break
                block_text = "".join(block)

                # 过滤
                if account_filter is None or account_filter in block_text:
                    print(f"  ─── {fpath.name} ───")
                    for line in block:
                        if line.strip():
                            print("  " + line, end="")
                    print()
                    found_any = True
                i = j
                continue
            i += 1

    if not found_any:
        acc_note = f"（涉及 {account_filter}）" if account_filter else ""
        print(f"  （该日无 bean 分录{acc_note}）")


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def inspect(period: str, target: date, account: str | None = None) -> None:
    import os
    os.chdir(ROOT)

    print(f"\n{'='*60}")
    print(f" 精查 {period} — {target}")
    if account:
        print(f" 账户过滤：{account}")
    print(f"{'='*60}\n")

    # 银行原始记录
    print("【银行原始记录】")
    _show_bank(period, target)
    print()

    # 支付宝原始记录
    print("【支付宝原始记录】")
    _show_source(period, "alipay", target)
    print()

    # 支付宝基金原始记录
    print("【支付宝基金原始记录】")
    _show_source(period, "alipay_funds", target)
    print()

    # 微信原始记录
    print("【微信原始记录】")
    _show_source(period, "wechat", target)
    print()

    # 京东原始记录
    print("【京东原始记录】")
    _show_source(period, "jingdong", target)
    print()

    # Bean 分录
    print(f"【.bean 分录" + (f"（{account}）" if account else "（全部）") + "】")
    _show_bean_entries(period, target, account)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="精查某一天的原始账单与 bean 分录对照"
    )
    parser.add_argument("period", help="账期，如 2507-2508")
    parser.add_argument("date", help="日期，如 2025-07-15")
    parser.add_argument("--account", "-a", help="只显示涉及该账户的 bean 分录", default=None)
    args = parser.parse_args()

    try:
        target = date.fromisoformat(args.date)
    except ValueError:
        sys.exit(f"错误：日期格式有误（应为 YYYY-MM-DD）：{args.date}")

    inspect(args.period, target, args.account)


if __name__ == "__main__":
    main()
