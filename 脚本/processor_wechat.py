#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]


def _find_column(df, target):
    if target in df.columns:
        return target
    for col in df.columns:
        if target in col:
            return col
    return None


def read_wechat_file(file_path):
    print(f"    读取微信文件: {os.path.basename(file_path)}")
    try:
        df_probe = pd.read_excel(file_path, header=None, nrows=30, dtype=str)
        header_row = None
        for i, row in df_probe.iterrows():
            if str(row[0]).strip() == '交易时间':
                header_row = i
                break
        if header_row is None:
            print("    ❌ 未找到列名行（'交易时间'）")
            return None
        df = pd.read_excel(file_path, skiprows=header_row, dtype=str)
    except Exception as e:
        print(f"    ❌ 读取失败: {e}")
        return None

    if df.empty:
        print("    ⚠️  文件读取后为空")
        return None

    df.columns = [str(col).replace('\n', '').replace(' ', '').strip() for col in df.columns]
    print(f"    原始列名: {list(df.columns)}")

    col_map = {
        '交易时间': '交易时间', '交易类型': '交易类型', '交易对方': '交易对方',
        '商品':     '商品',     '收/支':    '收/支',    '金额(元)': '金额(元)',
        '支付方式': '支付方式', '当前状态': '当前状态', '交易单号': '交易单号',
        '商户单号': '商户单号', '备注':     '备注',
    }
    std = pd.DataFrame()
    for raw_col, std_col in col_map.items():
        matched = _find_column(df, raw_col)
        std[std_col] = df[matched] if matched else ''
    std['数据来源'] = '微信'

    std['交易时间'] = std['交易时间'].astype(str).str.strip()
    std = std[std['交易时间'].str.match(r'\d{4}-\d{2}-\d{2}', na=False)]

    std['金额(元)'] = (
        std['金额(元)'].astype(str)
        .str.replace('¥', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    std['金额(元)'] = pd.to_numeric(std['金额(元)'], errors='coerce')
    std = std.dropna(subset=['金额(元)']).reset_index(drop=True)

    for col in STANDARD_COLUMNS:
        if col not in std.columns:
            std[col] = ''

    result = std[STANDARD_COLUMNS] if not std.empty else None
    if result is not None:
        print(f"    ✅ 成功处理 {len(result)} 条记录")
    return result


def main():
    ROOT_DIR    = Path(__file__).parent.parent
    WECHAT_DIR  = ROOT_DIR / "原始数据" / "微信" / PERIOD
    RESULT_DIR  = ROOT_DIR / "整理后数据" / PERIOD
    OUTPUT_FILE = RESULT_DIR / f"微信_{PERIOD}.xlsx"

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n📅 当前处理周期: 20{PERIOD[:2]}年{PERIOD[2:]}月")

    if not WECHAT_DIR.exists():
        print("❌ 微信目录不存在:", WECHAT_DIR)
        return

    files = [f for f in os.listdir(WECHAT_DIR) if f.lower().endswith(('.xlsx', '.xls'))]
    if not files:
        print("❌ 未找到 Excel 文件")
        return
    if len(files) > 1:
        print("⚠️  目录中存在多个 Excel 文件，请确认只有一个")
        return

    df = read_wechat_file(WECHAT_DIR / files[0])
    if df is None:
        print("❌ 处理失败")
        return

    df.to_excel(OUTPUT_FILE, index=False)
    if not validate_output(str(OUTPUT_FILE)):
        print("❌ 微信输出验证失败")
        return
    print(f"\n✅ 输出完成: {OUTPUT_FILE}\n📊 共 {len(df)} 条记录\n")


if __name__ == "__main__":
    main()
