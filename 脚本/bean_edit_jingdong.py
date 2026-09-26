#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, ACCOUNTS, get_jingdong_expense_account, get_payment_account, sanitize_bean_df

INPUT_PATH  = f"整理后数据/{PERIOD}/京东_{PERIOD}.xlsx"
OUTPUT_PATH = f"bean_files/{PERIOD}/jingdong_{PERIOD}.bean"
os.makedirs(f"bean_files/{PERIOD}", exist_ok=True)

def _pay(text): return get_payment_account(str(text), source="all")
def fmt(amount): return f"{float(amount):.2f} CNY"

def build_meta(row):
    lines = []
    col_map = {'交易时间':'time','交易类型':'category','交易对方':'payee_raw',
               '商品':'goods','收/支':'flow','当前状态':'status','备注':'note','数据来源':'source'}
    for col, key in col_map.items():
        val = row.get(col, '')
        if pd.notna(val) and str(val).strip() not in ('', 'nan', '/'):
            lines.append(f'  {key}: "{str(val).strip().replace(chr(34), chr(92)+chr(34))}"')
    return lines

def make_txn(date, flag, payee, narration, legs, meta, links=None):
    link_str = (" " + " ".join(f"^{l}" for l in links)) if links else ""
    payee_part = f'"{payee}" ' if payee else ""
    header = f'{date} {flag} {payee_part}"{narration}"{link_str}'
    lines  = [header] + meta
    for account, amount in legs:
        lines.append(f"  {account:<55} {amount}")
    return "\n".join(lines)

def main():
    if not os.path.exists(INPUT_PATH):
        print(f"❌ 找不到: {INPUT_PATH}"); return

    df_raw = sanitize_bean_df(pd.read_excel(INPUT_PATH, dtype=str))
    df_raw['金额(元)'] = pd.to_numeric(df_raw['金额(元)'], errors='coerce').fillna(0)
    df_raw = df_raw.fillna('')
    excel_row_count = len(df_raw)

    rows_processed         = set()
    rows_skipped_xianxiang = set()
    timed_entries          = []

    df = df_raw.sort_values('交易时间', ascending=True).reset_index()

    # 退款配对
    nc = df[df['收/支'] == '不计收支']
    orig_by_order, refund_by_order = {}, {}
    orig_idx_by_order, refund_idx_by_order = {}, {}

    for _, row in nc.iterrows():
        oid = str(row.get('交易单号', '')).strip()
        if not oid: continue
        goods, status = str(row.get('商品', '')), str(row.get('当前状态', ''))
        if "退款" in goods and status == "退款成功":
            refund_by_order[oid], refund_idx_by_order[oid] = row, row['index']
        elif "退款" not in goods:
            orig_by_order[oid], orig_idx_by_order[oid] = row, row['index']

    for oid in set(orig_by_order) & set(refund_by_order):
        orig, refund = orig_by_order[oid], refund_by_order[oid]
        expense_acc = get_jingdong_expense_account(str(orig.get('交易类型', '')))
        rows_processed.add(orig_idx_by_order[oid])
        rows_processed.add(refund_idx_by_order[oid])
        timed_entries.append((str(orig['交易时间']), make_txn(
            str(orig['交易时间'])[:10], "*", str(orig['交易对方']), str(orig['商品']),
            [(expense_acc, fmt(orig['金额(元)'])), (_pay(str(orig['支付方式'])), fmt(-orig['金额(元)']))],
            build_meta(orig), [oid]
        )))
        timed_entries.append((str(refund['交易时间']), make_txn(
            str(refund['交易时间'])[:10], "*", str(refund['交易对方']), str(refund['商品']),
            [(_pay(str(refund['支付方式'])), fmt(refund['金额(元)'])), (expense_acc, fmt(-refund['金额(元)']))],
            build_meta(refund), [oid]
        )))

    # 主循环
    for _, row in df.iterrows():
        idx = row['index']
        if idx in rows_processed: continue

        date       = str(row['交易时间'])[:10]
        flow       = str(row.get('收/支', '')).strip()
        trade_type = str(row.get('交易类型', '')).strip()
        goods      = str(row.get('商品', ''))
        pay_method = str(row.get('支付方式', ''))
        status     = str(row.get('当前状态', ''))
        amount     = float(row.get('金额(元)', 0))
        payee      = str(row.get('交易对方', ''))
        meta       = build_meta(row)
        txn        = None

        if flow == '收入':
            txn = make_txn(date, "*", payee, goods,
                           [(_pay(pay_method), fmt(amount)), (ACCOUNTS["unknown_income"], fmt(-amount))], meta)
        elif flow == '不计收支':
            if "白条" in trade_type:
                txn = make_txn(date, "*", payee, goods,
                               [(ACCOUNTS["jd_baitiao"], fmt(amount)), (_pay(pay_method), fmt(-amount))], meta)
            elif "先享后付" in pay_method and "退款" not in goods:
                rows_skipped_xianxiang.add(idx)
                rows_processed.add(idx)
                continue
            elif "退款" in goods and status == "退款成功":
                txn = make_txn(date, "*", payee, goods,
                               [(_pay(pay_method), fmt(amount)), (ACCOUNTS["unknown_expense"], fmt(-amount))], meta)
            else:
                txn = make_txn(date, "*", payee, goods,
                               [(ACCOUNTS["unknown_equity"], fmt(amount)), (_pay(pay_method), fmt(-amount))], meta)
        elif flow == '支出':
            txn = make_txn(date, "*", payee, goods,
                           [(get_jingdong_expense_account(trade_type), fmt(amount)), (_pay(pay_method), fmt(-amount))], meta)

        if txn:
            timed_entries.append((str(row['交易时间']), txn))
            rows_processed.add(idx)

    count_done = len(rows_processed)
    is_perfect = (count_done == excel_row_count)

    audit_header = (
        f"; ==================================================\n"
        f"; 京东账单 {PERIOD} 自动生成 - 审计面板\n"
        f"; 1. Excel 原始总行数:          {excel_row_count}\n"
        f"; 2. 成功转换处理行数:          {count_done}\n"
        f"; 3. 跳过(先享后付/不计收支):  {len(rows_skipped_xianxiang)}\n"
        f"; --------------------------------------------------\n"
        f"; 状态: {'✅ 完美对应' if is_perfect else '❌ 存在未处理行'}\n"
        f"; ==================================================\n\n"
    )

    timed_entries.sort(key=lambda x: x[0])
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(audit_header)
        f.write("\n\n".join([e[1] for e in timed_entries]) + "\n")
        if not is_perfect:
            missing = set(df['index']) - rows_processed
            f.write("\n\n; ⚠️ 未处理行：\n")
            for m_idx in missing:
                m_row = df[df['index'] == m_idx].iloc[0]
                f.write(f"; [行ID:{m_idx}] {m_row['交易时间']} | {m_row['商品']} | {m_row['收/支']} | {m_row['金额(元)']} CNY\n")

    print(f"📊 审计: {count_done}/{excel_row_count} (完美: {is_perfect})")
    if not is_perfect:
        print(f"❌ 发现 {len(set(df['index']) - rows_processed)} 行丢失！明细已写入 .bean 文件末尾。")

if __name__ == "__main__":
    main()
