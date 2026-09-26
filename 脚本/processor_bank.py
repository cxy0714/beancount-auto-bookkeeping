#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import pandas as pd
import pdfplumber
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output, BANK_CARDS, find_card_by_full_account, find_raw_dir

BANK_NAME = "示例银行A"
# 仅处理示例银行A下载的「交易流水明细*.pdf」；其他银行 PDF 由对应处理器接管
BANK_A_FILENAME_PREFIX = "交易流水明细"

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '余额', '币别', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]

# 币别中文 → ISO 货币代码（commodity）
CURRENCY_MAP = {
    "人民币": "CNY",
    "欧元":   "EUR",
    "美元":   "USD",
}


def _find_column(df, candidates):
    for name in candidates:
        if name in df.columns:
            return name
        for col in df.columns:
            if name in str(col):
                return col
    return None


_CARD_NO_PAT = re.compile(r"借记卡号[：:]\s*([0-9]+)")


def _extract_card_number(pdf) -> str | None:
    """从 PDF 首页文字中解析「借记卡号」完整号码。"""
    txt = pdf.pages[0].extract_text() or ""
    m = _CARD_NO_PAT.search(txt)
    return m.group(1) if m else None


def read_bank_pdf(file_path):
    print(f"    [Step 1] 正在读取 PDF: {os.path.basename(file_path)}")
    all_tables = []
    card_full_no = None

    try:
        with pdfplumber.open(file_path) as pdf:
            card_full_no = _extract_card_number(pdf)
            for page in pdf.pages:
                tables = page.extract_tables()
                for table in tables:
                    if table:
                        all_tables.append(pd.DataFrame(table))
    except Exception as e:
        print(f"    ❌ PDF 读取失败: {e}")
        return None, None

    if not all_tables:
        return None, card_full_no

    df = pd.concat(all_tables, ignore_index=True)

    header_row_idx = None
    for i, row in df.iterrows():
        if '记账日期' in ''.join(map(str, row)):
            header_row_idx = i
            break

    if header_row_idx is not None:
        df.columns = df.iloc[header_row_idx].fillna('').astype(str).str.strip().str.replace('\n', '')
        df = df.iloc[header_row_idx + 1:].reset_index(drop=True)

    amount_col = _find_column(df, ['金额'])
    if not amount_col:
        print('    ❌ 未能找到「金额」列，请检查 PDF 格式')
        return None, card_full_no

    df[amount_col] = df[amount_col].astype(str).str.replace(',', '').str.strip()
    df = df[pd.to_numeric(df[amount_col], errors='coerce').notna()].copy()

    print(f"    [Step 2] 清洗完成，有效交易记录: {len(df)} 条")

    std = pd.DataFrame()

    date_c = _find_column(df, ['记账日期'])
    time_c = _find_column(df, ['记账时间'])
    std['交易时间'] = (
        df[date_c].fillna('').astype(str).str.strip() + " " +
        df[time_c].fillna('').astype(str).str.strip()
    )

    std['交易类型'] = df[_find_column(df, ['交易名称', '摘要'])].astype(str).str.strip()
    std['交易对方'] = df[_find_column(df, ['对方账户名', '对方户名'])].astype(str).str.strip()
    std['商品']     = df[_find_column(df, ['附言', '备注'])].astype(str).str.strip()

    numeric_amount  = pd.to_numeric(df[amount_col])
    std['金额(元)'] = numeric_amount.abs()
    std['收/支']    = numeric_amount.apply(lambda x: '收入' if x > 0 else '支出')

    balance_col = _find_column(df, ['余额'])
    if balance_col:
        std['余额'] = pd.to_numeric(
            df[balance_col].astype(str).str.replace(',', '').str.strip(),
            errors='coerce'
        )
    else:
        std['余额'] = None

    currency_col = _find_column(df, ['币别', '币种'])
    if currency_col:
        std['币别'] = df[currency_col].astype(str).str.strip().map(
            lambda v: CURRENCY_MAP.get(v, v) if v else 'CNY'
        )
    else:
        std['币别'] = 'CNY'

    std['支付方式'] = df[_find_column(df, ['渠道'])] if _find_column(df, ['渠道']) else '银行卡'
    std['当前状态'] = '交易成功'
    card_suffix = (card_full_no or "")[-4:] if card_full_no else "????"
    std['数据来源'] = f"{BANK_NAME}{card_suffix}"

    account_col = _find_column(df, ['对方卡号/账号', '对方账号', '对方卡号'])
    std['商户单号'] = df[account_col].astype(str).str.strip() if account_col else ''

    bank_branch_col  = _find_column(df, ['对方开户行'])
    local_branch_col = _find_column(df, ['网点名称'])
    branch_info = df[bank_branch_col].fillna('').astype(str).str.strip() if bank_branch_col else ""
    local_info  = df[local_branch_col].fillna('').astype(str).str.strip() if local_branch_col else ""
    std['备注'] = (branch_info + " | " + local_info).str.strip(' |')  # type: ignore

    for col in STANDARD_COLUMNS:
        if col not in std.columns:
            std[col] = ''

    return std[STANDARD_COLUMNS], card_full_no


def main():
    ROOT_DIR       = Path(__file__).parent.parent
    BANK_INPUT_DIR = find_raw_dir("示例银行A", PERIOD)
    RESULT_DIR     = ROOT_DIR / "整理后数据" / PERIOD
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    if not BANK_INPUT_DIR or not BANK_INPUT_DIR.exists():
        print(f"❌ 找不到输入目录: 原始数据/示例银行A/{PERIOD}")
        return

    pdf_files = sorted(
        f for f in os.listdir(BANK_INPUT_DIR)
        if f.lower().endswith('.pdf') and f.startswith(BANK_A_FILENAME_PREFIX)
    )
    if not pdf_files:
        print(f"❌ 在 {BANK_INPUT_DIR} 下没找到以「{BANK_A_FILENAME_PREFIX}」开头的 PDF 文件")
        return

    ok = 0
    skipped = 0
    for fn in pdf_files:
        df_result, card_full_no = read_bank_pdf(BANK_INPUT_DIR / fn)
        if df_result is None:
            print(f"  ❌ 处理失败: {fn}")
            skipped += 1
            continue

        card_id = find_card_by_full_account(card_full_no) if card_full_no else None
        if not card_id:
            print(f"  ⚠️  未识别卡号 ({card_full_no!r})，跳过: {fn}")
            print(f"     如是新卡，请在 config.BANK_CARDS 添加注册")
            skipped += 1
            continue

        out_file = RESULT_DIR / f"银行卡{card_id}_{PERIOD}.xlsx"
        df_result.to_excel(out_file, index=False)
        if not validate_output(str(out_file)):
            print(f"  ❌ 输出验证失败: {out_file}")
            skipped += 1
            continue
        print(f"  ✅ {fn} → 银行卡{card_id}_{PERIOD}.xlsx ({len(df_result)} 行)")
        ok += 1

    print(f"\n📊 共处理 {ok} 张卡，跳过 {skipped} 个 PDF")
    if ok == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
