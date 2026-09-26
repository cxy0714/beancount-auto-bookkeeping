#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
信用卡 PDF 账单标准化处理器（示例银行A信用卡 9001 专用）

输入：原始数据/示例银行A信用卡/{YYMM}/示例银行A信用卡电子合并账单*.PDF
      （信用卡按月归档，与其他账单不同。PERIOD 跨多月时按月展开。）
输出：整理后数据/{PERIOD}/信用卡{CARD_SUFFIX}_{PERIOD}.xlsx

约定：
- 信用卡从 2503 才开始使用，比 2503 早的月份自动跳过。
- 比如 PERIOD=2507-2508 会同时读取 信用卡/2507 和 信用卡/2508 两个文件夹。
"""

import os
import re
import sys
import pandas as pd
import pdfplumber
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output

BANK_NAME         = "示例银行A信用卡"
CARD_SUFFIX       = "9001"
CREDIT_CARD_START = "2501"   # 信用卡启用月份（YYMM），更早的月份跳过

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '余额', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]


def _parse_statement_date(filename: str):
    """从文件名提取账单年月，如 '...2026年04月账单.PDF' -> (2026, 4)。"""
    m = re.search(r'(\d{4})年(\d{1,2})月', filename)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


def _to_iso_date(date_str: str, stmt_year: int, stmt_month: int):
    """信用卡 PDF 中日期可能是 'YYYY-MM-DD' 或 'MM/DD'，统一转 'YYYY-MM-DD'。"""
    s = str(date_str).strip()
    if re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s
    m = re.match(r'^(\d{1,2})/(\d{1,2})$', s)
    if m and stmt_year and stmt_month:
        mm, dd = int(m.group(1)), int(m.group(2))
        # 账单覆盖上月22日至本月21日；MM == 账单月份则用账单年；否则跨年回退
        if mm == stmt_month:
            year = stmt_year
        else:
            year = stmt_year if stmt_month != 1 else stmt_year - 1
        return f"{year:04d}-{mm:02d}-{dd:02d}"
    return None


def read_credit_card_pdf(file_path: Path):
    print(f"    [Step 1] 正在读取 PDF: {file_path.name}")
    stmt_year, stmt_month = _parse_statement_date(file_path.name)

    rows = []
    try:
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if not table:
                        continue
                    # 仅处理 6 列交易表
                    if not all(len(r) == 6 for r in table if r):
                        continue
                    for r in table:
                        if not r or not r[0]:
                            continue
                        date_iso = _to_iso_date(r[0], stmt_year, stmt_month)
                        if not date_iso:
                            continue
                        desc = str(r[3] or '').replace('\n', '').strip()
                        deposit_raw = str(r[4] or '').replace(',', '').strip()
                        spend_raw   = str(r[5] or '').replace(',', '').strip()
                        try:
                            deposit = float(deposit_raw) if deposit_raw else 0.0
                            spend   = float(spend_raw)   if spend_raw   else 0.0
                        except ValueError:
                            continue
                        if deposit == 0.0 and spend == 0.0:
                            continue
                        if deposit > 0:
                            direction, amount = "收入", deposit
                        else:
                            direction, amount = "支出", spend
                        rows.append({
                            '交易时间': f"{date_iso} 00:00:00",
                            '交易类型': desc,
                            '交易对方': desc,
                            '商品':     desc,
                            '收/支':    direction,
                            '金额(元)': amount,
                            '余额':     None,
                            '支付方式': f"{BANK_NAME}{CARD_SUFFIX}",
                            '当前状态': "交易成功",
                            '交易单号': '',
                            '商户单号': '',
                            '备注':     '',
                            '数据来源': f"{BANK_NAME}{CARD_SUFFIX}",
                        })
    except Exception as e:
        print(f"    ❌ PDF 读取失败: {e}")
        return None

    if not rows:
        print("    ❌ 未能从 PDF 中解析出任何交易")
        return None

    df = pd.DataFrame(rows, columns=STANDARD_COLUMNS)
    print(f"    [Step 2] 清洗完成，有效交易记录: {len(df)} 条")
    return df


def _expand_period_months(period: str) -> list[str]:
    """将 PERIOD 展开成月份列表。

    '2509'      -> ['2509']
    '2507-2508' -> ['2507', '2508']
    '2502-2506' -> ['2502', '2503', '2504', '2505', '2506']
    """
    if '-' in period:
        start, end = period.split('-', 1)
    else:
        start = end = period
    sy, sm = int(start[:2]), int(start[2:])
    ey, em = int(end[:2]), int(end[2:])
    months: list[str] = []
    y, m = sy, sm
    while (y, m) <= (ey, em):
        months.append(f"{y:02d}{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    return months


def main():
    ROOT_DIR    = Path(__file__).parent.parent
    CC_ROOT     = ROOT_DIR / "原始数据" / BANK_NAME
    RESULT_DIR  = ROOT_DIR / "整理后数据" / PERIOD
    OUTPUT_FILE = RESULT_DIR / f"信用卡{CARD_SUFFIX}_{PERIOD}.xlsx"

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    all_months = _expand_period_months(PERIOD)
    target_months = [m for m in all_months if m >= CREDIT_CARD_START]
    skipped_early = [m for m in all_months if m < CREDIT_CARD_START]
    if skipped_early:
        print(f"  ℹ 跳过早于 {CREDIT_CARD_START} 的月份（信用卡未启用）: {', '.join(skipped_early)}")

    if not target_months:
        print(f"❌ PERIOD={PERIOD} 范围内没有可处理的信用卡月份（启用日期 {CREDIT_CARD_START}）")
        return

    dfs: list[pd.DataFrame] = []
    for m in target_months:
        month_dir = CC_ROOT / m
        if not month_dir.exists():
            print(f"  ⚠ 跳过：找不到目录 {month_dir}")
            continue
        pdf_files = [f for f in os.listdir(month_dir) if f.lower().endswith('.pdf')]
        if not pdf_files:
            print(f"  ⚠ 跳过：{month_dir} 下没找到 PDF 文件")
            continue
        df = read_credit_card_pdf(month_dir / pdf_files[0])
        if df is not None and not df.empty:
            dfs.append(df)

    if not dfs:
        print(f"❌ {PERIOD}：没有任何月份成功解析出信用卡交易")
        return

    df_result = pd.concat(dfs, ignore_index=True)
    df_result.to_excel(OUTPUT_FILE, index=False)
    if not validate_output(str(OUTPUT_FILE)):
        print("❌ 信用卡输出验证失败")
        return
    print(f"✅ 处理成功！合并 {len(dfs)} 个月共 {len(df_result)} 条 → {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
