#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import pandas as pd
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, ACCOUNTS, get_payment_account, sanitize_bean_df

# ──────────────────────────────────────────
# 路径
# ──────────────────────────────────────────
INPUT_PATH  = f"整理后数据/{PERIOD}/微信_{PERIOD}.xlsx"
OUTPUT_PATH = f"bean_files/{PERIOD}/wechat_{PERIOD}.bean"
os.makedirs(f"bean_files/{PERIOD}", exist_ok=True)

META_MAP = {
    '交易时间': 'time',  '交易类型': 'type',   '交易对方': 'payee_raw',
    '商品':     'goods', '收/支':    'flow',    '当前状态': 'status',
    '备注':     'remark','数据来源': 'source',
}

def _pay(text, fallback=None):
    result = get_payment_account(str(text), source="wechat")
    if result == ACCOUNTS["unknown_equity"] and fallback:
        return fallback
    return result

def parse_service_fee(remark_str):
    if "服务费" in remark_str:
        m = re.search(r'¥([\d\.]+)', remark_str)
        if m:
            return float(m.group(1))
    return 0.0

def classify_expense(row):
    counterparty = str(row["交易对方"])
    product      = str(row["商品"])
    if "示例洗衣" in counterparty:         return "Expenses:Service"
    if "示例机构" in counterparty:
        return "Expenses:Food" if "餐" in product else "Expenses:Service"
    return ACCOUNTS["unknown_expense"]

def resolve_income_account(row):
    pay_method = str(row["支付方式"])
    status     = str(row["当前状态"])
    acc = get_payment_account(pay_method, source="wechat")
    if acc != ACCOUNTS["unknown_equity"]:
        return acc
    if "已存入零钱" in status:   return ACCOUNTS["wechat_lingqian"]
    if "已转入零钱通" in status: return ACCOUNTS["wechat_lingqiantong"]
    return ACCOUNTS["wechat_lingqian"]

def main():
    if not os.path.exists(INPUT_PATH):
        print("❌ 未找到文件:", INPUT_PATH); return

    df_raw = sanitize_bean_df(pd.read_excel(INPUT_PATH))
    excel_row_count = len(df_raw)
    rows_processed  = set()
    rows_jd         = set()  # 京东相关 - 由京东账单负责

    df = df_raw.fillna("/")
    df["交易时间"] = pd.to_datetime(df["交易时间"])
    df = df.sort_values(by=["交易时间", "交易单号"]).reset_index()

    all_entries = []

    for _, row in df.iterrows():
        orig_idx  = row['index']
        date      = row["交易时间"].strftime("%Y-%m-%d")
        amount    = abs(row["金额(元)"])
        payee     = str(row['交易对方'])
        tx_type   = str(row['交易类型'])
        flow      = str(row['收/支'])
        remark    = str(row['备注'])
        narration = str(row['商品']) if row['商品'] != "/" else tx_type

        # 由京东账单负责的行需跳过，避免重复记账。判定条件（任一）：
        # 1. 支付方式含「网银在线」或「京东」（京东白条/京东支付）
        # 2. 交易对方含「京东商城」（京东线上商城的微信快捷支付订单，
        #    支付方式是普通银行卡，无法通过支付方式区分，但商户名固定）
        # 3. 商品含「京东-订单编号」（线上订单，交易对方可能只写"京东"不含"商城"，
        #    但订单号说明它在京东账单内，否则与京东侧重复扣款）
        # 注意：「京东便利店」线下门店用微信支付，无订单编号、交易对方不含"京东商城"，不跳过。
        pay_method_text = str(row.get('支付方式', ''))
        payee_text = str(row.get('交易对方', ''))
        goods_text = str(row.get('商品', ''))
        if ("网银在线" in pay_method_text or "京东" in pay_method_text
                or "京东商城" in payee_text or "京东-订单编号" in goods_text
                or "京东快递" in payee_text or "京东物流" in payee_text):
            rows_jd.add(orig_idx); continue

        entry_lines = [f'{date} * "{payee}" "{narration}"']
        for cn, en in META_MAP.items():
            val = str(row.get(cn, ""))
            # 去掉换行 + 压空白，避免影响 fava 预览
            val = val.replace('\r', ' ').replace('\n', ' ')
            val = ' '.join(val.split()).strip()
            if val and val not in ["nan", "/"]:
                entry_lines.append(f'  {en}: "{val}"')

        if flow == "支出":
            pay_acc = get_payment_account(row["支付方式"], source="wechat")
            if pay_acc == ACCOUNTS["unknown_equity"]:
                pay_acc = ACCOUNTS["wechat_lingqian"]
            exp_acc = classify_expense(row)
            entry_lines.append(f'  {exp_acc:<55} {amount:>10.2f} CNY')
            entry_lines.append(f'  {pay_acc}')
            rows_processed.add(orig_idx)

        elif flow == "收入":
            asset_acc = resolve_income_account(row)
            # 对端账户按交易类型路由（不再一律 Expenses:Unknown）：
            #   微信红包 → Income:Other
            #   群收款/转账/二维码收款 → Equity:Transfer（个人之间结算/代收付，非真实收入）
            #   其余 → Income:Unknown（待 reclassifier 细化）
            if "红包" in tx_type:
                counter_acc = ACCOUNTS["income_luck"]
            elif any(k in tx_type for k in ("群收款", "转账", "二维码收款", "收款")):
                counter_acc = ACCOUNTS["equity_transfer"]
            else:
                counter_acc = ACCOUNTS["unknown_income"]
            entry_lines.append(f'  {asset_acc:<55} {amount:>10.2f} CNY')
            entry_lines.append(f'  {counter_acc}')
            rows_processed.add(orig_idx)

        elif flow == "/":
            fee        = parse_service_fee(remark)
            net_amount = amount - fee
            from_acc   = ACCOUNTS["unknown_equity"]
            to_acc     = ACCOUNTS["unknown_equity"]

            # 中性划转：零钱/零钱通/银行卡 之间。微信账单「交易类型」用词多样：
            #   零钱充值（银行卡→零钱）/ 零钱提现（零钱→银行卡）
            #   转入零钱通-来自零钱 / 零钱转入零钱通（零钱→零钱通）
            #   零钱通转出-到零钱 / 零钱通转出-到示例银行A(xxxx)（零钱通→零钱/银行卡）
            # 指向银行卡的腿用支付方式解析真实卡（回溯账期落 Equity:Transfer，银行侧已全量入账）。
            bank_leg = get_payment_account(row["支付方式"], source="wechat")
            if "零钱充值" in tx_type:
                from_acc = bank_leg
                to_acc   = ACCOUNTS["wechat_lingqian"]
            elif "零钱提现" in tx_type:
                from_acc = ACCOUNTS["wechat_lingqian"]
                to_acc   = bank_leg
            elif "转入零钱通" in tx_type or "零钱转入零钱通" in tx_type:
                from_acc = ACCOUNTS["wechat_lingqian"]
                to_acc   = ACCOUNTS["wechat_lingqiantong"]
            elif "零钱通转出" in tx_type:
                from_acc = ACCOUNTS["wechat_lingqiantong"]
                to_acc   = ACCOUNTS["wechat_lingqian"] if "到零钱" in tx_type else bank_leg

            entry_lines.append(f'  {to_acc:<55} {net_amount:>10.2f} CNY')
            if fee > 0:
                entry_lines.append(f'  {"Expenses:Fees":<55} {fee:>10.2f} CNY')
            entry_lines.append(f'  {from_acc:<55} {-amount:>10.2f} CNY')
            rows_processed.add(orig_idx)

        all_entries.append("\n".join(entry_lines) + "\n")

    count_done = len(rows_processed)
    count_jd   = len(rows_jd)
    total      = count_done + count_jd
    is_perfect = (total == excel_row_count)

    audit_header = (
        f"; ==================================================\n"
        f"; 微信 {PERIOD} 自动生成 - 审计面板\n"
        f"; 1. Excel 原始总行数:     {excel_row_count}\n"
        f"; 2. 成功解析处理行数:     {count_done}\n"
        f"; 3. 忽略（京东账单负责）: {count_jd}\n"
        f"; 4. 未能处理行数:         {excel_row_count - total}\n"
        f"; --------------------------------------------------\n"
        f"; [核对] {count_done} + {count_jd} = {total}\n"
        f"; 状态: {'✅ 完美对应' if is_perfect else '❌ 存在差额'}\n"
        f"; ==================================================\n\n"
    )

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        f.write(audit_header)
        f.write("\n".join(all_entries))

    print(f"✅ 微信转换完成！审计结果: {total}/{excel_row_count}")
    if not is_perfect:
        handled = rows_processed | rows_jd
        missing = set(df_raw.index) - handled
        print(f"\n⚠️  [失踪行] {len(missing)} 行：")
        print(df_raw.loc[list(missing)][["交易时间","交易类型","商品","金额(元)","收/支"]].to_string())

if __name__ == "__main__":
    main()