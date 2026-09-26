#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信用卡 Excel 转 Beancount 脚本（示例银行A信用卡 9001 专用）

逻辑（参考 bean_edit_bank.py）：
- 信用卡支出中带「支付宝-」「网银在线-」「微信-」前缀的交易，已在对应账单里记录，本侧跳过
- 信用卡存入中「支付宝CHN」「网银在线CHN」「微信CHN」也是 Alipay/JD/微信 渠道退款，跳过
- 「BANKANET」「还款成功」等还款流水由银行卡 1001 一侧记录，本侧跳过
- 其余支出（如携程订酒店/机票、抖音支付等无法被其他账单覆盖的）才落账到 Liabilities:CreditCard:9001
"""

import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, ACCOUNTS

BANK_TAG    = "banka"
CARD_SUFFIX = "9001"

META_MAP = {
    '交易时间': 'time',  '交易类型': 'type',   '交易对方': 'payee_raw',
    '商品':     'goods', '收/支':    'flow',   '当前状态': 'status',
    '备注':     'remark','数据来源': 'source',
}


def main():
    ROOT_DIR    = Path(__file__).parent.parent
    INPUT_PATH  = ROOT_DIR / "整理后数据" / PERIOD / f"信用卡{CARD_SUFFIX}_{PERIOD}.xlsx"
    OUTPUT_PATH = ROOT_DIR / "bean_files" / PERIOD / f"creditcard_{BANK_TAG}_{CARD_SUFFIX}_{PERIOD}.bean"
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    MY_CARD = ACCOUNTS["credit_9001"]
    UNKNOWN_EXPENSE = ACCOUNTS["unknown_expense"]
    UNKNOWN_INCOME  = ACCOUNTS["unknown_income"]

    if not INPUT_PATH.exists():
        print(f"❌ 找不到: {INPUT_PATH}"); return

    df_raw = pd.read_excel(INPUT_PATH)
    excel_row_count = len(df_raw)

    rows_alipay    = set()
    rows_wechat    = set()
    rows_jd        = set()
    rows_repay     = set()  # 信用卡还款 - 已在银行卡 1001 侧处理
    rows_processed = set()

    df = df_raw.fillna("")
    df['交易时间'] = pd.to_datetime(df['交易时间'])
    df = df.sort_values(by='交易时间').reset_index()

    lines = []

    for _, row in df.iterrows():
        orig_idx  = row['index']
        desc      = str(row['商品']).strip()
        direction = str(row['收/支'])
        amount    = float(row['金额(元)'])

        # 还款流水（已在 1001 银行卡侧记录）
        if desc.startswith("BANKANET") or "还款" in desc:
            rows_repay.add(orig_idx); continue

        # 支付宝渠道：含「支付宝」前缀或单独的「支付宝CHN」退款
        if desc.startswith("支付宝-") or desc == "支付宝CHN" or desc.startswith("支付宝CHN"):
            rows_alipay.add(orig_idx); continue

        # 京东渠道：「网银在线-xxx」消费 或 「网银在线CHN」退款
        if desc.startswith("网银在线-") or desc == "网银在线CHN" or desc.startswith("网银在线CHN"):
            rows_jd.add(orig_idx); continue

        # 微信渠道：「微信-xxx」消费 或 「微信CHN」退款
        if desc.startswith("微信-") or desc == "微信CHN" or desc.startswith("微信CHN"):
            rows_wechat.add(orig_idx); continue

        pay_date = row['交易时间'].strftime('%Y-%m-%d')
        target = UNKNOWN_INCOME if direction == "收入" else UNKNOWN_EXPENSE

        entry = [f'{pay_date} * "{desc}" "{desc}"']
        for cn, en in META_MAP.items():
            val = str(row.get(cn, ""))
            val = val.replace('\r', ' ').replace('\n', ' ')
            val = ' '.join(val.split()).strip()
            if val and val not in ["nan", ""]:
                entry.append(f'  {en}: "{val}"')

        if direction == "收入":
            entry.append(f'  {MY_CARD:<55} {amount:>10.2f} CNY')
            entry.append(f'  {target}')
        else:
            entry.append(f'  {MY_CARD:<55} {-amount:>10.2f} CNY')
            entry.append(f'  {target}')

        lines.append("\n".join(entry) + "\n")
        rows_processed.add(orig_idx)

    count_ali   = len(rows_alipay)
    count_we    = len(rows_wechat)
    count_jd    = len(rows_jd)
    count_repay = len(rows_repay)
    count_done  = len(rows_processed)
    total       = count_ali + count_we + count_jd + count_repay + count_done

    audit_header = (
        f"; ==================================================\n"
        f"; CREDITCARD {CARD_SUFFIX} 信用卡账单 - 审计面板\n"
        f"; 1. Excel 原始总行数:     {excel_row_count}\n"
        f"; 2. 转换为 Bean 条目:     {count_done}\n"
        f"; 3. 忽略（避免重复流水）:\n"
        f";     - 支付宝 (Alipay):    {count_ali}\n"
        f";     - 微信:               {count_we}\n"
        f";     - 京东 (网银在线):    {count_jd}\n"
        f";     - 还款 / BANKANET:      {count_repay}\n"
        f"; --------------------------------------------------\n"
        f"; [核对] {count_done} + {count_ali} + {count_we} + {count_jd} + {count_repay} = {total}\n"
        f"; 状态: {'✅ 完美对应' if total == excel_row_count else '❌ 存在差额'}\n"
        f"; ==================================================\n\n"
    )

    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(audit_header)
        f.write("\n".join(lines))

    print(f"✅ 信用卡转换完成！对账结果: {total}/{excel_row_count}")
    if total != excel_row_count:
        handled = rows_processed | rows_alipay | rows_wechat | rows_jd | rows_repay
        missing = set(df_raw.index) - handled
        print(f"\n⚠️  [失踪行] {len(missing)} 行：")
        print(df_raw.loc[list(missing)].to_string())


# 兼容直接运行旧名称
convert_to_beancount = main

if __name__ == "__main__":
    main()
